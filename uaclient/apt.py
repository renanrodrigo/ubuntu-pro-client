import copy
import datetime
import enum
import glob
import logging
import os
import re
import subprocess
import tempfile
from functools import lru_cache, wraps
from typing import Dict, Iterable, List, NamedTuple, Optional, Set, Union

import apt_pkg  # type: ignore
from apt.progress.base import AcquireProgress  # type: ignore

from uaclient import (
    event_logger,
    exceptions,
    gpg,
    messages,
    secret_manager,
    system,
    util,
)
from uaclient.defaults import ESM_APT_ROOTDIR
from uaclient.files.state_files import status_cache_file

APT_HELPER_TIMEOUT = 60.0  # 60 second timeout used for apt-helper call
APT_AUTH_COMMENT = "  # ubuntu-pro-client"
APT_CONFIG_AUTH_FILE = "Dir::Etc::netrc/"
APT_CONFIG_AUTH_PARTS_DIR = "Dir::Etc::netrcparts/"
APT_CONFIG_LISTS_DIR = "Dir::State::lists/"
APT_PROXY_CONFIG_HEADER = """\
/*
 * Autogenerated by ubuntu-pro-client
 * Do not edit this file directly
 *
 * To change what ubuntu-pro-client sets, use the `pro config set`
 * or the `pro config unset` commands to set/unset either:
 *      global_apt_http_proxy and global_apt_https_proxy
 * for a global apt proxy
 * or
 *      ua_apt_http_proxy and ua_apt_https_proxy
 * for an apt proxy that only applies to Ubuntu Pro related repos.
 */
"""
APT_CONFIG_GLOBAL_PROXY_HTTP = """Acquire::http::Proxy "{proxy_url}";\n"""
APT_CONFIG_GLOBAL_PROXY_HTTPS = """Acquire::https::Proxy "{proxy_url}";\n"""
APT_CONFIG_UA_PROXY_HTTP = (
    """Acquire::http::Proxy::esm.ubuntu.com "{proxy_url}";\n"""
)
APT_CONFIG_UA_PROXY_HTTPS = (
    """Acquire::https::Proxy::esm.ubuntu.com "{proxy_url}";\n"""
)
APT_KEYS_DIR = "/etc/apt/trusted.gpg.d/"
KEYRINGS_DIR = "/usr/share/keyrings"
APT_METHOD_HTTPS_FILE = "/usr/lib/apt/methods/https"
CA_CERTIFICATES_FILE = "/usr/sbin/update-ca-certificates"
APT_PROXY_CONF_FILE = "/etc/apt/apt.conf.d/90ubuntu-advantage-aptproxy"

APT_UPDATE_SUCCESS_STAMP_PATH = "/var/lib/apt/periodic/update-success-stamp"

SERIES_NOT_USING_DEB822 = ("xenial", "bionic", "focal", "jammy", "mantic")

DEB822_REPO_FILE_CONTENT = """\
# Written by ubuntu-pro-client
Types: deb{deb_src}
URIs: {url}
Suites: {suites}
Components: main
Signed-By: {keyrings_dir}/{keyring_file}
"""


ESM_BASIC_FILE_STRUCTURE = {
    "files": [
        os.path.join(ESM_APT_ROOTDIR, "etc/apt/sources.list"),
        os.path.join(ESM_APT_ROOTDIR, "var/lib/dpkg/status"),
    ],
    "folders": [
        os.path.join(ESM_APT_ROOTDIR, "var/cache/apt/archives/partial"),
        os.path.join(ESM_APT_ROOTDIR, "var/lib/apt/lists/partial"),
    ],
}

# Since we generally have a person at the command line prompt. Don't loop
# for 5 minutes like charmhelpers because we expect the human to notice and
# resolve to apt conflict or try again.
# Hope for an optimal first try.
APT_RETRIES = [1.0, 5.0, 10.0]

event = event_logger.get_event_logger()
LOG = logging.getLogger(util.replace_top_level_logger_name(__name__))


@enum.unique
class AptProxyScope(enum.Enum):
    GLOBAL = object()
    UACLIENT = object()


InstalledAptPackage = NamedTuple(
    "InstalledAptPackage", [("name", str), ("version", str), ("arch", str)]
)


def ensure_apt_pkg_init(f):
    """Decorator ensuring apt_pkg is initialized."""

    @wraps(f)
    def new_f(*args, **kwargs):
        # This call is checking for the 'Dir' configuration - which needs to be
        # there for apt_pkg to be ready - and if it is empty we initialize.
        if apt_pkg.config.get("Dir") == "":
            apt_pkg.init()
        return f(*args, **kwargs)

    return new_f


@ensure_apt_pkg_init
def version_compare(a: str, b: str):
    return apt_pkg.version_compare(a, b)


def assert_valid_apt_credentials(repo_url, username, password):
    """Validate apt credentials for a PPA.

    @param repo_url: private-ppa url path
    @param username: PPA login username.
    @param password: PPA login password or resource token.

    @raises: UbuntuProError for invalid credentials, timeout or unexpected
        errors.
    """
    protocol, repo_path = repo_url.split("://")
    if not os.path.exists("/usr/lib/apt/apt-helper"):
        return
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            system.subp(
                [
                    "/usr/lib/apt/apt-helper",
                    "download-file",
                    "{}://{}:{}@{}/pool/".format(
                        protocol, username, password, repo_path
                    ),
                    os.path.join(tmpd, "apt-helper-output"),
                ],
                timeout=APT_HELPER_TIMEOUT,
                retry_sleeps=APT_RETRIES,
            )
    except exceptions.ProcessExecutionError as e:
        if e.exit_code == 100:
            stderr = str(e.stderr).lower()
            if re.search(r"401\s+unauthorized|httperror401", stderr):
                raise exceptions.APTInvalidCredentials(repo=repo_url)
            elif re.search(r"connection timed out", stderr):
                raise exceptions.APTTimeout(repo=repo_url)
        raise exceptions.APTUnexpectedError(detail=str(e))
    except subprocess.TimeoutExpired:
        raise exceptions.APTCommandTimeout(
            seconds=APT_HELPER_TIMEOUT, repo=repo_path
        )


def _parse_apt_update_for_invalid_apt_config(
    apt_error: str,
) -> Set[str]:
    """Parse apt update errors for invalid apt config in user machine.

    This functions parses apt update errors regarding the presence of
    invalid apt config in the system, for example, a ppa that cannot be
    reached, for example.

    In that scenario, apt will output a message in the following formats:

    The repository 'ppa 404 Release' ...
    Failed to fetch ppa 404 ...

    On some releases, both of these errors will be present in the apt error
    message.

    :param apt_error: The apt error string
    :return: a NamedMessage containing the error message
    """
    failed_repos = set()

    for line in apt_error.strip().split("\n"):
        if line:
            pattern_match = re.search(
                r"(Failed to fetch |The repository .)(?P<url>[^\s]+)", line
            )

            if pattern_match:
                repo_url_match = (
                    "- " + pattern_match.groupdict()["url"].split("/dists")[0]
                )

                failed_repos.add(repo_url_match)

    return failed_repos


def run_apt_command(
    cmd: List[str],
    error_msg: Optional[str] = None,
    override_env_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Run an apt command, retrying upon failure APT_RETRIES times.

    :param cmd: List containing the apt command to run, passed to subp.
    :param error_msg: The string to raise as UbuntuProError when all retries
       are exhausted in failure.
    :param override_env_vars: Passed directly as subp's override_env_vars arg

    :return: stdout from successful run of the apt command.
    :raise UbuntuProError: on issues running apt-cache policy.
    """
    try:
        out, _err = system.subp(
            cmd,
            capture=True,
            retry_sleeps=APT_RETRIES,
            override_env_vars=override_env_vars,
        )
    except exceptions.ProcessExecutionError as e:
        if "Could not get lock /var/lib/dpkg/lock" in str(e.stderr):
            raise exceptions.APTProcessConflictError()
        else:
            """
            Treat errors where one of the APT repositories
            is invalid or unreachable. In that situation, we alert
            which repository is causing the error
            """
            failed_repos = _parse_apt_update_for_invalid_apt_config(e.stderr)
            if failed_repos:
                raise exceptions.APTInvalidRepoError(
                    failed_repos="\n".join(sorted(failed_repos))
                )

        msg = error_msg if error_msg else str(e)
        raise exceptions.APTUnexpectedError(detail=msg)
    return out


@lru_cache(maxsize=None)
def get_apt_cache_policy(
    error_msg: Optional[str] = None,
    override_env_vars: Optional[Dict[str, str]] = None,
) -> str:
    return run_apt_command(
        cmd=["apt-cache", "policy"],
        error_msg=error_msg,
        override_env_vars=override_env_vars,
    )


class PreserveAptCfg:
    def __init__(self, apt_func):
        self.apt_func = apt_func
        self.current_apt_cfg = {}  # Dict[str, Any]

    def __enter__(self):
        cfg = apt_pkg.config
        self.current_apt_cfg = {
            key: copy.deepcopy(cfg.get(key)) for key in cfg.keys()
        }

        return self.apt_func()

    def __exit__(self, type, value, traceback):
        cfg = apt_pkg.config
        # We need to restore the apt cache configuration after creating our
        # cache, otherwise we may break people interacting with the
        # library after importing our modules.
        for key in self.current_apt_cfg.keys():
            cfg.set(key, self.current_apt_cfg[key])
        apt_pkg.init_system()


def get_apt_pkg_cache():
    for key in apt_pkg.config.keys():
        apt_pkg.config.clear(key)
    apt_pkg.init()
    return apt_pkg.Cache(None)


def get_esm_apt_pkg_cache():
    try:
        # Take care to initialize the cache with only the
        # Acquire configuration preserved
        for key in apt_pkg.config.keys():
            if not re.search("^Acquire", key):
                apt_pkg.config.clear(key)
        apt_pkg.config.set("Dir", ESM_APT_ROOTDIR)
        apt_pkg.init()
        # If the rootdir folder doesn't contain any apt source info, the
        # cache will be empty
        # If the structure in the rootdir folder does not exist or is
        # incorrect, an exception will be raised
        return apt_pkg.Cache(None)
    except Exception:
        # The empty dictionary will act as an empty cache
        return {}


def get_pkg_version(pkg_name: str) -> Optional[str]:
    with PreserveAptCfg(get_apt_pkg_cache) as cache:
        try:
            package = cache[pkg_name]
        except KeyError:
            return None

    if package.current_ver:
        return package.current_ver.ver_str

    return None


def get_pkg_candidate_version(
    pkg_name: str, check_esm_cache: bool = False
) -> Optional[str]:
    with PreserveAptCfg(get_apt_pkg_cache) as cache:
        try:
            package = cache[pkg_name]
        except KeyError:
            return None

        dep_cache = apt_pkg.DepCache(cache)
        candidate = dep_cache.get_candidate_ver(package)
        if not candidate:
            return None

        candidate_version = candidate.ver_str

    if not check_esm_cache:
        return candidate_version

    with PreserveAptCfg(get_esm_apt_pkg_cache) as esm_cache:
        if esm_cache:
            try:
                esm_package = esm_cache[pkg_name]
            except KeyError:
                return candidate_version

            esm_dep_cache = apt_pkg.DepCache(esm_cache)
            esm_candidate = esm_dep_cache.get_candidate_ver(esm_package)
            if not esm_candidate:
                return candidate_version

            esm_candidate_version = esm_candidate.ver_str

            if (
                apt_pkg.version_compare(
                    esm_candidate_version, candidate_version
                )
                >= 0
            ):
                return esm_candidate_version

    return candidate_version


def run_apt_update_command(
    override_env_vars: Optional[Dict[str, str]] = None
) -> str:
    try:
        out = run_apt_command(
            cmd=["apt-get", "update"], override_env_vars=override_env_vars
        )
    except exceptions.APTProcessConflictError:
        raise exceptions.APTUpdateProcessConflictError()
    except exceptions.APTInvalidRepoError as e:
        raise exceptions.APTUpdateInvalidRepoError(repo_msg=e.msg)
    except exceptions.UbuntuProError as e:
        raise exceptions.APTUpdateFailed(detail=e.msg)
    finally:
        # Whenever we run an apt-get update command, we must invalidate
        # the existing apt-cache policy cache. Otherwise, we could provide
        # users with incorrect values.
        get_apt_cache_policy.cache_clear()

    return out


@util.retry(
    (exceptions.APTProcessConflictError, exceptions.APTUpdateFailed),
    APT_RETRIES,
)
def update_sources_list(sources_list_path: str):
    with PreserveAptCfg(get_apt_pkg_cache) as cache:
        # Configure the sources_list to be updated
        apt_pkg.config.set(
            "Dir::Etc::sourcelist", os.path.abspath(sources_list_path)
        )
        # When going for a specific sourcelist, we don't care about sourceparts
        # and thus we set it to NOFOLDER. We hope that users don't have a
        # directory called N.O.F.O.L.D.E.R in /etc/apt/ or wherever their
        # apt config is defined
        apt_pkg.config.set("Dir::Etc::sourceparts", "N.O.F.O.L.D.E.R")
        apt_pkg.config.set("APT::List-Cleanup", "0")
        sources_list = apt_pkg.SourceList()
        sources_list.read_main_list()

        # We need a fetch progress monitor, so we create an empty one
        # No way to run from apt here, as apt_pkg itself uses this class
        fetch_progress = AcquireProgress()

        # Configure the apt lock
        lock_file = os.path.join(
            apt_pkg.config.find_dir("Dir::State::Lists"), "lock"
        )
        lock = apt_pkg.FileLock(lock_file)

        try:
            with lock:
                cache.update(fetch_progress, sources_list, 0)
        # No apt_pkg.Error on Xenial
        except getattr(apt_pkg, "Error", ()):
            raise exceptions.APTProcessConflictError()
        except SystemError as e:
            raise exceptions.APTUpdateFailed(detail=str(e))
        finally:
            get_apt_cache_policy.cache_clear()


def run_apt_install_command(
    packages: List[str],
    apt_options: Optional[List[str]] = None,
    override_env_vars: Optional[Dict[str, str]] = None,
) -> str:
    if apt_options is None:
        apt_options = []

    try:
        out = run_apt_command(
            cmd=["apt-get", "install", "--assume-yes"]
            + apt_options
            + packages,
            override_env_vars=override_env_vars,
        )
    except exceptions.APTProcessConflictError:
        raise exceptions.APTInstallProcessConflictError()
    except exceptions.APTInvalidRepoError as e:
        raise exceptions.APTInstallInvalidRepoError(repo_msg=e.msg)

    return out


def get_installed_packages_by_origin(origin: str) -> List[apt_pkg.Package]:
    # Avoiding duplicate entries, which may happen due to version being in
    # multiple pockets or supporting multiple architectures.
    result = set()
    with PreserveAptCfg(get_apt_pkg_cache) as cache:
        for package in cache.packages:
            installed_version = package.current_ver
            if installed_version:
                for file, _ in installed_version.file_list:
                    if file.origin == origin:
                        result.add(package)

    return list(result)


def get_remote_versions_for_package(
    package: apt_pkg.Package, exclude_origin: Optional[str] = None
) -> List[apt_pkg.Version]:
    valid_versions = []
    for version in package.version_list:
        valid_origins = [
            file
            for file, _ in version.file_list
            # component == now means we are getting it from the local dpkg
            # cache, and we don't really care about those entries because
            # they are the currently installed version of the package.
            if file.component != "now" and file.origin != exclude_origin
        ]
        if valid_origins:
            valid_versions.append(version)

    return valid_versions


def _get_list_file_content(
    suites: List[str], series: str, updates_enabled: bool, repo_url: str
) -> str:
    content = ""
    for suite in suites:
        if series not in suite:
            continue  # Only enable suites matching this current series
        maybe_comment = ""
        if "-updates" in suite and not updates_enabled:
            LOG.warning(
                'Not enabling apt suite "%s" because "%s-updates" is not'
                " enabled",
                suite,
                series,
            )
            maybe_comment = "# "
        content += (
            "{maybe_comment}deb {url} {suite} main\n"
            "# deb-src {url} {suite} main\n".format(
                maybe_comment=maybe_comment, url=repo_url, suite=suite
            )
        )

    return content


def _get_sources_file_content(
    suites: List[str],
    series: str,
    updates_enabled: bool,
    repo_url: str,
    keyring_file: str,
    include_deb_src: bool = False,
) -> str:
    appliable_suites = [suite for suite in suites if series in suite]
    if not updates_enabled:
        LOG.warning(
            "Not enabling service-related -updates suites because"
            ' "%s-updates" is not enabled',
            series,
        )
        appliable_suites = [
            suite for suite in appliable_suites if "-updates" not in suite
        ]

    deb_src = " deb-src" if include_deb_src else ""

    content = DEB822_REPO_FILE_CONTENT.format(
        url=repo_url,
        suites=" ".join(appliable_suites),
        keyrings_dir=KEYRINGS_DIR,
        keyring_file=keyring_file,
        deb_src=deb_src,
    )

    return content


def add_auth_apt_repo(
    repo_filename: str,
    repo_url: str,
    credentials: str,
    suites: List[str],
    keyring_file: str,
) -> None:
    """Add an authenticated apt repo and credentials to the system.

    @raises: InvalidAPTCredentialsError when the token provided can't access
        the repo PPA.
    """
    try:
        username, password = credentials.split(":")
    except ValueError:  # Then we have a bearer token
        username = "bearer"
        password = credentials
    secret_manager.secrets.add_secret(password)
    series = system.get_release_info().series
    if repo_url.endswith("/"):
        repo_url = repo_url[:-1]
    assert_valid_apt_credentials(repo_url, username, password)

    # Does this system have updates suite enabled?
    updates_enabled = False
    policy = run_apt_command(
        ["apt-cache", "policy"], messages.APT_POLICY_FAILED
    )
    for line in policy.splitlines():
        # We only care about $suite-updates lines
        if "a={}-updates".format(series) not in line:
            continue
        # We only care about $suite-updates from the Ubuntu archive
        if "o=Ubuntu," not in line:
            continue
        updates_enabled = True
        break

    add_apt_auth_conf_entry(repo_url, username, password)

    if series in SERIES_NOT_USING_DEB822:
        source_keyring_file = os.path.join(KEYRINGS_DIR, keyring_file)
        destination_keyring_file = os.path.join(APT_KEYS_DIR, keyring_file)
        gpg.export_gpg_key(source_keyring_file, destination_keyring_file)

        content = _get_list_file_content(
            suites, series, updates_enabled, repo_url
        )
    else:
        content = _get_sources_file_content(
            suites, series, updates_enabled, repo_url, keyring_file
        )

    system.write_file(repo_filename, content)


def add_apt_auth_conf_entry(repo_url, login, password):
    """Add or replace an apt auth line in apt's auth.conf file or conf.d."""
    apt_auth_file = get_apt_auth_file_from_apt_config()
    _protocol, repo_path = repo_url.split("://")
    if not repo_path.endswith("/"):  # ensure trailing slash
        repo_path += "/"
    if os.path.exists(apt_auth_file):
        orig_content = system.load_file(apt_auth_file)
    else:
        orig_content = ""

    repo_auth_line = (
        "machine {repo_path} login {login} password {password}"
        "{cmt}".format(
            repo_path=repo_path,
            login=login,
            password=password,
            cmt=APT_AUTH_COMMENT,
        )
    )
    added_new_auth = False
    new_lines = []
    for line in orig_content.splitlines():
        if not added_new_auth:
            split_line = line.split()
            if len(split_line) >= 2:
                curr_line_repo = split_line[1]
                if curr_line_repo == repo_path:
                    # Replace old auth with new auth at same line
                    new_lines.append(repo_auth_line)
                    added_new_auth = True
                    continue
                if curr_line_repo in repo_path:
                    # Insert our repo before.
                    # We are a more specific apt repo match
                    new_lines.append(repo_auth_line)
                    added_new_auth = True
        new_lines.append(line)
    if not added_new_auth:
        new_lines.append(repo_auth_line)
    new_lines.append("")
    system.write_file(apt_auth_file, "\n".join(new_lines), mode=0o600)


def remove_repo_from_apt_auth_file(repo_url):
    """Remove a repo from the shared apt auth file"""
    _protocol, repo_path = repo_url.split("://")
    if repo_path.endswith("/"):  # strip trailing slash
        repo_path = repo_path[:-1]
    apt_auth_file = get_apt_auth_file_from_apt_config()
    if os.path.exists(apt_auth_file):
        apt_auth = system.load_file(apt_auth_file)
        auth_prefix = "machine {repo_path}/ login".format(repo_path=repo_path)
        content = "\n".join(
            [line for line in apt_auth.splitlines() if auth_prefix not in line]
        )
        if not content:
            system.ensure_file_absent(apt_auth_file)
        else:
            system.write_file(apt_auth_file, content, mode=0o600)


def remove_auth_apt_repo(
    repo_filename: str, repo_url: str, keyring_file: Optional[str] = None
) -> None:
    """Remove an authenticated apt repo and credentials to the system"""
    system.ensure_file_absent(repo_filename)
    # Also try to remove old .list files for compatibility with older releases.
    if repo_filename.endswith(".sources"):
        system.ensure_file_absent(
            util.set_filename_extension(repo_filename, "list")
        )

    if keyring_file:
        keyring_file = os.path.join(APT_KEYS_DIR, keyring_file)
        system.ensure_file_absent(keyring_file)
    remove_repo_from_apt_auth_file(repo_url)


def add_ppa_pinning(apt_preference_file, repo_url, origin, priority):
    """Add an apt preferences file and pin for a PPA."""
    _protocol, repo_path = repo_url.split("://")
    if repo_path.endswith("/"):  # strip trailing slash
        repo_path = repo_path[:-1]
    content = (
        "Package: *\n"
        "Pin: release o={origin}\n"
        "Pin-Priority: {priority}\n".format(origin=origin, priority=priority)
    )
    system.write_file(apt_preference_file, content)


def get_apt_auth_file_from_apt_config():
    """Return to patch to the system configured APT auth file."""
    out, _err = system.subp(
        ["apt-config", "shell", "key", APT_CONFIG_AUTH_PARTS_DIR]
    )
    if out:  # then auth.conf.d parts is present
        return out.split("'")[1] + "90ubuntu-advantage"
    else:  # then use configured /etc/apt/auth.conf
        out, _err = system.subp(
            ["apt-config", "shell", "key", APT_CONFIG_AUTH_FILE]
        )
        return out.split("'")[1].rstrip("/")


def find_apt_list_files(repo_url, series):
    """List any apt files in APT_CONFIG_LISTS_DIR given repo_url and series."""
    _protocol, repo_path = repo_url.split("://")
    if repo_path.endswith("/"):  # strip trailing slash
        repo_path = repo_path[:-1]
    lists_dir = "/var/lib/apt/lists"
    out, _err = system.subp(
        ["apt-config", "shell", "key", APT_CONFIG_LISTS_DIR]
    )
    if out:  # then lists dir is present in config
        lists_dir = out.split("'")[1]

    aptlist_filename = repo_path.replace("/", "_")
    return sorted(
        glob.glob(
            os.path.join(
                lists_dir, aptlist_filename + "_dists_{}*".format(series)
            )
        )
    )


def remove_apt_list_files(repo_url, series):
    """Remove any apt list files present for this repo_url and series."""
    for path in find_apt_list_files(repo_url, series):
        system.ensure_file_absent(path)


def is_installed(pkg: str) -> bool:
    return pkg in get_installed_packages_names()


def get_installed_packages() -> List[InstalledAptPackage]:
    out, _ = system.subp(["apt", "list", "--installed"])
    package_list = out.splitlines()[1:]
    return [
        InstalledAptPackage(
            name=entry.split("/")[0],
            version=entry.split(" ")[1],
            arch=entry.split(" ")[2],
        )
        for entry in package_list
    ]


def get_installed_packages_names() -> List[str]:
    package_list = get_installed_packages()
    pkg_names = [pkg.name for pkg in package_list]
    return pkg_names


def setup_apt_proxy(
    http_proxy: Optional[str] = None,
    https_proxy: Optional[str] = None,
    proxy_scope: Optional[AptProxyScope] = AptProxyScope.GLOBAL,
) -> None:
    """
    Writes an apt conf file that configures apt to use the proxies provided as
    args.
    If both args are None, then no apt conf file is written. If this function
    previously wrote a conf file, and was run again with both args as None,
    the existing file is removed.

    :param http_proxy: the url of the http proxy apt should use, or None
    :param https_proxy: the url of the https proxy apt should use, or None
    :return: None
    """
    if http_proxy or https_proxy:
        if proxy_scope:
            message = ""
            if proxy_scope == AptProxyScope.UACLIENT:
                message = "UA-scoped"
            elif proxy_scope == AptProxyScope.GLOBAL:
                message = "global"
            event.info(
                messages.SETTING_SERVICE_PROXY_SCOPE.format(scope=message)
            )

    apt_proxy_config = ""
    if http_proxy:
        if proxy_scope == AptProxyScope.UACLIENT:
            apt_proxy_config += APT_CONFIG_UA_PROXY_HTTP.format(
                proxy_url=http_proxy
            )
        elif proxy_scope == AptProxyScope.GLOBAL:
            apt_proxy_config += APT_CONFIG_GLOBAL_PROXY_HTTP.format(
                proxy_url=http_proxy
            )
    if https_proxy:
        if proxy_scope == AptProxyScope.UACLIENT:
            apt_proxy_config += APT_CONFIG_UA_PROXY_HTTPS.format(
                proxy_url=https_proxy
            )
        elif proxy_scope == AptProxyScope.GLOBAL:
            apt_proxy_config += APT_CONFIG_GLOBAL_PROXY_HTTPS.format(
                proxy_url=https_proxy
            )

    if apt_proxy_config != "":
        apt_proxy_config = APT_PROXY_CONFIG_HEADER + apt_proxy_config

    if apt_proxy_config == "":
        system.ensure_file_absent(APT_PROXY_CONF_FILE)
    else:
        system.write_file(APT_PROXY_CONF_FILE, apt_proxy_config)


def get_apt_cache_time() -> Optional[float]:
    cache_time = None
    if os.path.exists(APT_UPDATE_SUCCESS_STAMP_PATH):
        cache_time = os.stat(APT_UPDATE_SUCCESS_STAMP_PATH).st_mtime
    return cache_time


def get_apt_cache_datetime() -> Optional[datetime.datetime]:
    cache_time = get_apt_cache_time()
    if cache_time is None:
        return None
    return datetime.datetime.fromtimestamp(cache_time, datetime.timezone.utc)


def _ensure_esm_cache_structure():
    # make sure all necessary files exist...
    existing_files = glob.glob(
        os.path.join(ESM_APT_ROOTDIR, "**/*"), recursive=True
    )
    desired_files = (
        ESM_BASIC_FILE_STRUCTURE["files"] + ESM_BASIC_FILE_STRUCTURE["folders"]
    )
    if all((file in existing_files for file in desired_files)):
        return

    # ...otherwise make sure they do NOT exist...
    system.ensure_folder_absent(ESM_APT_ROOTDIR)

    # ...and recreate them
    for file in ESM_BASIC_FILE_STRUCTURE["files"]:
        system.create_file(file)
    for folder in ESM_BASIC_FILE_STRUCTURE["folders"]:
        os.makedirs(folder, exist_ok=True, mode=755)


def update_esm_caches(cfg) -> None:
    if not system.is_current_series_lts():
        return

    _ensure_esm_cache_structure()

    from uaclient.actions import status
    from uaclient.entitlements.entitlement_status import ApplicationStatus
    from uaclient.entitlements.esm import (
        ESMAppsEntitlement,
        ESMInfraEntitlement,
    )

    apps_available = False
    infra_available = False

    current_status = status_cache_file.read()
    if current_status is None:
        current_status = status(cfg)[0]

    for service in current_status.get("services", []):
        if service.get("name", "") == "esm-apps":
            apps_available = service.get("available", "no") == "yes"
        if service.get("name", "") == "esm-infra":
            infra_available = service.get("available", "no") == "yes"

    apps = ESMAppsEntitlement(cfg)

    # Always setup ESM-Apps
    if (
        apps_available
        and apps.application_status()[0] == ApplicationStatus.DISABLED
    ):
        apps.setup_local_esm_repo()
    else:
        apps.disable_local_esm_repo()

    # Only setup ESM-Infra for EOSS systems
    if system.is_current_series_active_esm():
        infra = ESMInfraEntitlement(cfg)
        if (
            infra_available
            and infra.application_status()[0] == ApplicationStatus.DISABLED
        ):
            infra.setup_local_esm_repo()
        else:
            infra.disable_local_esm_repo()

    # Read the cache and update it
    with PreserveAptCfg(get_esm_apt_pkg_cache) as cache:
        sources_list = apt_pkg.SourceList()
        sources_list.read_main_list()

        class EsmAcquireProgress(AcquireProgress):
            def done(self, item: apt_pkg.AcquireItemDesc):
                LOG.debug("Fetched ESM Apt Cache item: {}".format(item.uri))

            def fail(self, item: apt_pkg.AcquireItemDesc):
                LOG.warning(
                    "Failed to fetch ESM Apt Cache item: {}".format(item.uri)
                )

        fetch_progress = EsmAcquireProgress()
        try:
            cache.update(fetch_progress, sources_list, 0)
        except SystemError as e:
            LOG.warning("Failed to fetch the ESM Apt Cache: {}".format(str(e)))


def remove_packages(package_names: List[str], error_message: str):
    """
    Remove APT packages from the system.

    Setting DEBIAN_FRONTEND to noninteractive makes sure no prompts will
    appear during the operation. In this case, --force-confdef will
    automatically pick the default option when some debconf should appear.
    In the absence of a default option, --force-confold will automatically
    choose to keep the old configuration file.
    """
    run_apt_command(
        [
            "apt-get",
            "remove",
            "--assume-yes",
            '-o Dpkg::Options::="--force-confdef"',
            '-o Dpkg::Options::="--force-confold"',
        ]
        + list(package_names),
        error_message,
        override_env_vars={"DEBIAN_FRONTEND": "noninteractive"},
    )


def purge_packages(package_names: List[str], error_message: str):
    """
    Purge APT packages from the system - remove everything.

    Setting DEBIAN_FRONTEND to noninteractive makes sure no prompts will
    appear during the operation. In this case, --force-confdef will
    automatically pick the default option when some debconf should appear.
    In the absence of a default option, --force-confold will automatically
    choose to keep the old configuration file.
    """
    run_apt_command(
        [
            "apt-get",
            "purge",
            "--assume-yes",
            '-o Dpkg::Options::="--force-confdef"',
            '-o Dpkg::Options::="--force-confold"',
        ]
        + list(package_names),
        error_message,
        override_env_vars={"DEBIAN_FRONTEND": "noninteractive"},
    )


def reinstall_packages(package_names: List[str]):
    """
    Install packages, allowing downgrades.

    The --allow downgrades flag is needed because sometimes we need to
    reinstall the packages to a lower version (passed in the package_name
    string, as package=version).

    Setting DEBIAN_FRONTEND to noninteractive makes sure no prompts will
    appear during the operation. In this case, --force-confdef will
    automatically pick the default option when some debconf should appear.
    In the absence of a default option, --force-confold will automatically
    choose to keep the old configuration file.
    """
    run_apt_install_command(
        package_names,
        apt_options=[
            "--allow-downgrades",
            '-o Dpkg::Options::="--force-confdef"',
            '-o Dpkg::Options::="--force-confold"',
        ],
        override_env_vars={"DEBIAN_FRONTEND": "noninteractive"},
    )


def _get_apt_config():
    # We need to clear the config values in case another module
    # has already initiated it
    for key in apt_pkg.config.keys():
        apt_pkg.config.clear(key)

    apt_pkg.init_config()
    return apt_pkg.config


def get_apt_config_keys(base_key):
    with PreserveAptCfg(_get_apt_config) as apt_cfg:
        apt_cfg_keys = apt_cfg.list(base_key)

    return apt_cfg_keys


def get_apt_config_values(
    cfg_names: Iterable[str],
) -> Dict[str, Union[str, List[str]]]:
    """
    Get all APT configuration values for the given config names. If
    one of the config names is not present on the APT config, that
    config name will have a value of None
    """
    apt_cfg_dict = {}  # type: Dict[str, Union[str, List[str]]]

    with PreserveAptCfg(_get_apt_config) as apt_cfg:
        for cfg_name in cfg_names:
            cfg_value = apt_cfg.get(cfg_name)

            if not str(cfg_value):
                cfg_value = apt_cfg.value_list(cfg_name) or None

            apt_cfg_dict[cfg_name] = cfg_value

    return apt_cfg_dict


def get_system_sources_file() -> str:
    old_sources_path = "/etc/apt/sources.list"
    new_sources_path = "/etc/apt/sources.list.d/ubuntu.sources"
    return (
        new_sources_path
        if os.path.exists(new_sources_path)
        else old_sources_path
    )
