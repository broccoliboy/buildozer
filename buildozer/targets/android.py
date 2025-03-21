'''
Android target, based on python-for-android project
'''

import os
import sys
if sys.platform == 'win32' and not os.getenv("KIVY_WIN32_ANDROID_EXPERIMENTAL"):
    raise NotImplementedError('Windows platform not yet working for Android')

from platform import uname
WSL = 'microsoft' in uname()[2].lower()

ANDROID_API = '31'
ANDROID_MINAPI = '21'
APACHE_ANT_VERSION = '1.9.4'

# This constant should *not* be updated, it is used only in the case
# that python-for-android cannot provide a recommendation, which in
# turn only happens if the python-for-android is old and probably
# doesn't support any newer NDK.
DEFAULT_ANDROID_NDK_VERSION = '17c'

import ast
from glob import glob
import io
from os import environ
from os.path import exists, join, realpath, expanduser, basename, relpath
from platform import architecture
import re
import shlex
from shutil import which
from sys import platform, executable
from time import sleep
import traceback

import packaging.version
import pexpect

import buildozer.buildops as buildops
from buildozer.exceptions import BuildozerException
from buildozer.logger import USE_COLOR
from buildozer.target import Target
from buildozer.libs.version import parse


# buildozer.spec tokens that used to exist but are now ignored
DEPRECATED_TOKENS = (('app', 'android.sdk'), )

# Default SDK tag to download. This is not a configurable option
# because it doesn't seem to matter much, it is normally correct to
# download once then update all the components as buildozer already
# does.
DEFAULT_SDK_TAG = '6514223'

DEFAULT_ARCHS = ['arm64-v8a', 'armeabi-v7a']

MSG_P4A_RECOMMENDED_NDK_ERROR = (
    "WARNING: Unable to find recommended Android NDK for current "
    "installation of python-for-android, defaulting to the default "
    "version r{android_ndk}".format(android_ndk=DEFAULT_ANDROID_NDK_VERSION)
)


class TargetAndroid(Target):
    targetname = 'android'
    p4a_directory_name = "python-for-android"
    p4a_fork = 'kivy'
    p4a_branch = 'master'
    p4a_commit = 'HEAD'
    p4a_recommended_ndk_version = None
    extra_p4a_args = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.artifact_format = 'apk'
        self._serials = None

        if self.buildozer.config.has_option(
            "app", "android.arch"
        ) and not self.buildozer.config.has_option("app", "android.archs"):
            self.logger.error("`android.archs` not detected, instead `android.arch` is present.")
            self.logger.error("`android.arch` will be removed and ignored in future.")
            self.logger.error("If you're seeing this error, please migrate to `android.archs`.")
            self._archs = self.buildozer.config.getlist(
                'app', 'android.arch', DEFAULT_ARCHS)
        else:
            self._archs = self.buildozer.config.getlist(
                'app', 'android.archs', DEFAULT_ARCHS)
        self._build_dir = join(
            self.buildozer.platform_dir, 'build-{}'.format(self.archs_snake))
        executable = sys.executable or 'python'
        self._p4a_cmd = [executable, "-m", "pythonforandroid.toolchain"]
        self._p4a_bootstrap = self.buildozer.config.getdefault(
            'app', 'p4a.bootstrap', 'sdl2')
        color = 'always' if USE_COLOR else 'never'
        self.extra_p4a_args = [f"--color={color}", f"--storage-dir={self._build_dir}"]

        # minapi should match ndk-api, so can use the same default if
        # nothing is specified
        ndk_api = self.buildozer.config.getdefault(
            'app', 'android.ndk_api', self.android_minapi)
        self.extra_p4a_args.append(f"--ndk-api={ndk_api}")

        hook = self.buildozer.config.getdefault("app", "p4a.hook", None)
        if hook is not None:
            self.extra_p4a_args.append(f"--hook={realpath(expanduser(hook))}")
        port = self.buildozer.config.getdefault('app', 'p4a.port', None)
        if port is not None:
            self.extra_p4a_args.append(f"--port={port}")

        setup_py = self.buildozer.config.getdefault('app', 'p4a.setup_py', False)
        if setup_py:
            self.extra_p4a_args.append("--use-setup-py")
        else:
            self.extra_p4a_args.append("--ignore-setup-py")

        activity_class_name = self.buildozer.config.getdefault(
            'app', 'android.activity_class_name', 'org.kivy.android.PythonActivity')
        if activity_class_name != 'org.kivy.android.PythonActivity':
            self.extra_p4a_args.append(f"--activity-class-name={activity_class_name}")

        if self.logger.log_level >= 2:
            self.extra_p4a_args.append("--debug")

        user_extra_p4a_args = self.buildozer.config.getdefault('app', 'p4a.extra_args', "")
        self.extra_p4a_args.extend(shlex.split(user_extra_p4a_args))

        self.warn_on_deprecated_tokens()

    def warn_on_deprecated_tokens(self):
        for section, token in DEPRECATED_TOKENS:
            value = self.buildozer.config.getdefault(section, token, None)
            if value is not None:
                error = ('WARNING: Config token {} {} is deprecated and ignored, '
                         'but you set value {}').format(section, token, value)
                self.logger.error(error)

    def _p4a(self, cmd, env, **kwargs):
        kwargs.setdefault('cwd', self.p4a_dir)
        return buildops.cmd(
            [*self._p4a_cmd, *cmd, *self.extra_p4a_args],
            env=env,
            **kwargs)

    @property
    def p4a_dir(self):
        """The directory where python-for-android is/will be installed."""

        # Default p4a dir
        p4a_dir = join(self.buildozer.platform_dir, self.p4a_directory_name)

        # Possibly overridden by user setting
        system_p4a_dir = self.buildozer.config.getdefault('app', 'p4a.source_dir')
        if system_p4a_dir:
            p4a_dir = expanduser(system_p4a_dir)

        return p4a_dir

    @property
    def p4a_recommended_android_ndk(self):
        """
        Return the p4a's recommended android's NDK version, depending on the
        p4a version used for our buildozer build. In case that we don't find
        it, we will return the buildozer's recommended one, defined by global
        variable `DEFAULT_ANDROID_NDK_VERSION`.
        """
        # make sure to read p4a version only the first time
        if self.p4a_recommended_ndk_version is not None:
            return self.p4a_recommended_ndk_version

        # check p4a's recommendation file, and in case that exists find the
        # recommended android's NDK version, otherwise return buildozer's one
        ndk_version = DEFAULT_ANDROID_NDK_VERSION
        rec_file = join(self.p4a_dir, "pythonforandroid", "recommendations.py")
        if not os.path.isfile(rec_file):
            self.logger.error(MSG_P4A_RECOMMENDED_NDK_ERROR)
            return ndk_version

        for line in open(rec_file, "r"):
            if line.startswith("RECOMMENDED_NDK_VERSION ="):
                ndk_version = line.replace(
                    "RECOMMENDED_NDK_VERSION =", "")
                # clean version of unwanted characters
                for i in {"'", '"', "\n", " "}:
                    ndk_version = ndk_version.replace(i, "")
                self.logger.info(
                    "Recommended android's NDK version by p4a is: {}".format(
                        ndk_version
                    )
                )
                self.p4a_recommended_ndk_version = ndk_version
                break
        return ndk_version

    def _sdkmanager(self, *args, **kwargs):
        """Call the sdkmanager in our Android SDK with the given arguments."""
        # Use the android-sdk dir as cwd by default
        android_sdk_dir = self.android_sdk_dir
        kwargs['cwd'] = kwargs.get('cwd', android_sdk_dir)
        command = [self.sdkmanager_path, f"--sdk_root={android_sdk_dir}", *args]

        if kwargs.pop('return_child', False):
            return buildops.cmd_expect(
                command, env=self.buildozer.environ, **kwargs)
        else:
            kwargs['get_stdout'] = kwargs.get('get_stdout', True)
            return buildops.cmd(command, env=self.buildozer.environ, **kwargs)

    @property
    def android_ndk_version(self):
        return self.buildozer.config.getdefault('app', 'android.ndk',
                                                self.p4a_recommended_android_ndk)

    @property
    def android_api(self):
        return self.buildozer.config.getdefault('app', 'android.api',
                                                ANDROID_API)

    @property
    def android_minapi(self):
        return self.buildozer.config.getdefault('app', 'android.minapi',
                                                ANDROID_MINAPI)

    @property
    def android_sdk_dir(self):
        directory = expanduser(self.buildozer.config.getdefault(
            'app', 'android.sdk_path', ''))
        if directory:
            return realpath(directory)
        return join(self.buildozer.global_platform_dir,
                    'android-sdk')

    @property
    def android_ndk_dir(self):
        directory = expanduser(self.buildozer.config.getdefault(
            'app', 'android.ndk_path', ''))
        if directory:
            return realpath(directory)
        version = self.buildozer.config.getdefault('app', 'android.ndk',
                                                   self.android_ndk_version)
        return join(self.buildozer.global_platform_dir,
                    'android-ndk-r{0}'.format(version))

    @property
    def apache_ant_dir(self):
        directory = expanduser(self.buildozer.config.getdefault(
            'app', 'android.ant_path', ''))
        if directory:
            return realpath(directory)
        version = self.buildozer.config.getdefault('app', 'android.ant',
                                                   APACHE_ANT_VERSION)
        return join(self.buildozer.global_platform_dir,
                    'apache-ant-{0}'.format(version))

    @property
    def sdkmanager_path(self):
        sdk_manager_name = (
            'sdkmanager.bat'
            if platform in ('win32', 'cygwin')
            else 'sdkmanager'
        )
        sdkmanager_path = join(
            self.android_sdk_dir, 'tools', 'bin', sdk_manager_name)
        if not os.path.isfile(sdkmanager_path):
            raise BuildozerException(
                ('sdkmanager path "{}" does not exist, sdkmanager is not'
                 ' installed'.format(sdkmanager_path)))
        return sdkmanager_path

    @property
    def archs_snake(self):
        return "_".join(self._archs)

    def check_requirements(self):
        if platform in ('win32', 'cygwin'):
            try:
                self._set_win32_java_home()
            except:
                traceback.print_exc()
            self.adb_executable = join(self.android_sdk_dir, 'platform-tools',
                                'adb.exe')
            self.javac_cmd = self._locate_java('javac.exe')
            self.keytool_cmd = self._locate_java('keytool.exe')
        # darwin, linux, freebsd
        else:
            self.adb_executable = join(self.android_sdk_dir, 'platform-tools', 'adb')
            self.javac_cmd = self._locate_java('javac')
            self.keytool_cmd = self._locate_java('keytool')

            # Check for C header <zlib.h>.
            is_debian_like = which("dpkg") is not None
            if is_debian_like and \
                    not buildops.file_exists('/usr/include/zlib.h'):
                raise BuildozerException(
                    'zlib headers must be installed, '
                    'run: sudo apt-get install zlib1g-dev')

            # Override the OS which `sdkmanager` should download the packages for.
            # This enables download and use of Linux binaries on FreeBSD.
            if platform.startswith('freebsd'):
                os.environ['REPO_OS_OVERRIDE'] = 'linux'

        # Adb arguments:
        adb_args = self.buildozer.config.getdefault(
            "app", "android.adb_args", "")
        self.adb_args = shlex.split(adb_args)

        # Need to add internally-installed ant to path for external tools
        # (like adb) to use
        self.buildozer.environ['PATH'] = os.pathsep.join(
            [
                join(self.apache_ant_dir, 'bin'),
                self.buildozer.environ['PATH'],
            ])

        buildops.checkbin('Git (git)', 'git')
        buildops.checkbin('Cython (cython)', 'cython')
        buildops.checkbin('Java compiler (javac)', self.javac_cmd)
        buildops.checkbin('Java keytool (keytool)', self.keytool_cmd)

    def _p4a_have_aab_support(self):
        returncode = self._p4a(
            ["aab", "-h"],
            break_on_error=False,
            env=self.buildozer.environ).return_code
        if returncode == 0:
            return True
        else:
            return False

    def _set_win32_java_home(self):
        if 'JAVA_HOME' in self.buildozer.environ:
            return
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\JavaSoft\Java Development Kit") as jdk:  # @UndefinedVariable
            current_version, _type = winreg.QueryValueEx(
                jdk, "CurrentVersion")  # @UndefinedVariable
            with winreg.OpenKey(jdk, current_version) as cv:  # @UndefinedVariable
                java_home, _type = winreg.QueryValueEx(
                    cv, "JavaHome")  # @UndefinedVariable
            self.buildozer.environ['JAVA_HOME'] = java_home

    def _locate_java(self, s):
        '''If JAVA_HOME is in the environ, return $JAVA_HOME/bin/s. Otherwise,
        return s.
        '''
        if 'JAVA_HOME' in self.buildozer.environ:
            return join(self.buildozer.environ['JAVA_HOME'], 'bin', s)
        else:
            return s

    def _install_apache_ant(self):
        ant_dir = self.apache_ant_dir
        if buildops.file_exists(ant_dir):
            self.logger.info('Apache ANT found at {0}'.format(ant_dir))
            return ant_dir

        if not os.path.exists(ant_dir):
            os.makedirs(ant_dir)

        self.logger.info('Android ANT is missing, downloading')
        archive = 'apache-ant-{0}-bin.tar.gz'.format(APACHE_ANT_VERSION)
        url = 'https://archive.apache.org/dist/ant/binaries/'
        buildops.download(
            url,
            archive,
            cwd=ant_dir)
        buildops.file_extract(
            archive,
            cwd=ant_dir,
            env=self.buildozer.environ)
        self.logger.info('Apache ANT installation done.')
        return ant_dir

    def _install_android_sdk(self):
        sdk_dir = self.android_sdk_dir
        if buildops.file_exists(sdk_dir):
            self.logger.info('Android SDK found at {0}'.format(sdk_dir))
            return sdk_dir

        self.logger.info('Android SDK is missing, downloading')
        if platform in ('win32', 'cygwin'):
            archive = 'commandlinetools-win-{}_latest.zip'.format(DEFAULT_SDK_TAG)
        elif platform in ('darwin', ):
            archive = 'commandlinetools-mac-{}_latest.zip'.format(DEFAULT_SDK_TAG)
        elif platform.startswith('linux') or platform.startswith('freebsd'):
            archive = 'commandlinetools-linux-{}_latest.zip'.format(DEFAULT_SDK_TAG)
        else:
            raise SystemError('Unsupported platform: {0}'.format(platform))

        if not os.path.exists(sdk_dir):
            os.makedirs(sdk_dir)

        url = 'https://dl.google.com/android/repository/'
        buildops.download(
            url,
            archive,
            cwd=sdk_dir)

        self.logger.info('Unpacking Android SDK')
        buildops.file_extract(
            archive,
            cwd=sdk_dir,
            env=self.buildozer.environ)

        self.logger.info('Android SDK tools base installation done.')

        return sdk_dir

    def _install_android_ndk(self):
        ndk_dir = self.android_ndk_dir
        if buildops.file_exists(ndk_dir):
            self.logger.info('Android NDK found at {0}'.format(ndk_dir))
            return ndk_dir

        import re
        _version = int(re.search(r'(\d+)', self.android_ndk_version).group(1))

        self.logger.info('Android NDK is missing, downloading')
        # Welcome to the NDK URL hell!
        # a list of all NDK URLs up to level 14 can be found here:
        #  https://gist.github.com/roscopecoltran/43861414fbf341adac3b6fa05e7fad08
        # it seems that from level 11 on the naming schema is consistent
        # from 10e on the URLs can be looked up at
        # https://developer.android.com/ndk/downloads/older_releases

        is_darwin = platform == 'darwin'
        is_linux = platform.startswith('linux')
        is_freebsd = platform.startswith('freebsd')

        if platform in ('win32', 'cygwin'):
            # Checking of 32/64 bits at Windows from: https://stackoverflow.com/a/1405971/798575
            import struct
            archive = 'android-ndk-r{0}-windows.zip'
            is_64 = (8 * struct.calcsize("P") == 64)
        elif is_darwin or is_linux or is_freebsd:
            _platform = 'linux' if (is_linux or is_freebsd) else 'darwin'
            if self.android_ndk_version in ['10c', '10d', '10e']:
                ext = 'bin'
            elif _version <= 10:
                ext = 'tar.bz2'
            else:
                ext = 'zip'
            archive = 'android-ndk-r{0}-' + _platform + '{1}.' + ext
            is_64 = ('64' in os.uname()[4])
        else:
            raise SystemError('Unsupported platform: {}'.format(platform))

        architecture = 'x86_64' if is_64 else 'x86'
        architecture = '' if _version >= 23 else f'-{architecture}'
        unpacked = 'android-ndk-r{0}'
        archive = archive.format(self.android_ndk_version, architecture)
        unpacked = unpacked.format(self.android_ndk_version)

        if _version >= 11:
            url = 'https://dl.google.com/android/repository/'
        else:
            url = 'https://dl.google.com/android/ndk/'

        buildops.download(url,
                                archive,
                                cwd=self.buildozer.global_platform_dir)

        self.logger.info('Unpacking Android NDK')
        buildops.file_extract(
            archive,
            cwd=self.buildozer.global_platform_dir,
            env=self.buildozer.environ)
        buildops.rename(
            unpacked,
            ndk_dir,
            cwd=self.buildozer.global_platform_dir)
        self.logger.info('Android NDK installation done.')
        return ndk_dir

    def _android_list_build_tools_versions(self):
        available_packages = self._sdkmanager('--list')

        lines = available_packages[0].split('\n')

        build_tools_versions = []

        for line in lines:
            if not line.strip().startswith('build-tools;'):
                continue
            package_name = line.strip().split(' ')[0]
            assert package_name.count(';') == 1, (
                'could not parse package "{}"'.format(package_name))
            version = package_name.split(';')[1]

            build_tools_versions.append(parse(version))

        return build_tools_versions

    def _android_update_sdk(self, *sdkmanager_commands):
        """Update the tools and package-tools if possible"""
        auto_accept_license = self.buildozer.config.getbooldefault(
            'app', 'android.accept_sdk_license', False)

        kwargs = {}
        if auto_accept_license:
            kwargs["return_child"] = True
        else:
            kwargs['show_output'] = True

        ret_child = self._sdkmanager(*sdkmanager_commands, **kwargs)

        if auto_accept_license:
            while ret_child.isalive():
                pexp_match = ret_child.expect(
                    ["(y/N)", pexpect.EOF, pexpect.TIMEOUT], timeout=300
                )
                if pexp_match == 0:
                    ret_child.sendline("y")

    def _read_version_subdir(self, *args):
        versions = []
        if not os.path.exists(join(*args)):
            self.logger.debug('build-tools folder not found {}'.format(join(
                *args)))
            return parse("0")
        for v in os.listdir(join(*args)):
            try:
                versions.append(parse(v))
            except:
                pass
        if not versions:
            self.logger.error(
                'Unable to find the latest version for {}'.format(join(*args)))
            return parse("0")
        return max(versions)

    def _find_latest_package(self, packages, key):
        package_versions = []
        for p in packages:
            if not p.startswith(key):
                continue
            version_string = p.split(key)[-1]
            version = parse(version_string)
            package_versions.append(version)
        if not package_versions:
            return
        return max(package_versions)

    def _install_android_packages(self):

        # if any of these values change into the buildozer.spec, retry the
        # update
        cache_key = 'android:sdk_installation'
        cache_value = [
            self.android_api, self.android_minapi, self.android_ndk_version,
            self.android_sdk_dir, self.android_ndk_dir
        ]
        if self.buildozer.state.get(cache_key, None) == cache_value:
            return True

        # 1. update the platform-tools package if needed

        skip_upd = self.buildozer.config.getbooldefault(
            'app', 'android.skip_update', False)

        if not skip_upd:
            self.logger.info('Installing/updating SDK platform tools if necessary')

            # just calling sdkmanager with the items will install them if necessary
            self._android_update_sdk('platform-tools')
            self._android_update_sdk('--update')
        else:
            self.logger.info('Skipping Android SDK update due to spec file setting')
            self.logger.info('Note: this also prevents installing missing '
                                'SDK components')

        # 2. install the latest build tool
        self.logger.info('Updating SDK build tools if necessary')
        installed_v_build_tools = self._read_version_subdir(self.android_sdk_dir,
                                                  'build-tools')
        available_v_build_tools = self._android_list_build_tools_versions()
        if not available_v_build_tools:
            self.logger.error('Did not find any build tools available to download')

        latest_v_build_tools = sorted(available_v_build_tools)[-1]
        if latest_v_build_tools > installed_v_build_tools:
            if not skip_upd:
                self._android_update_sdk(f"build-tools;{latest_v_build_tools}")
                installed_v_build_tools = latest_v_build_tools
            else:
                self.logger.info(
                    'Skipping update to build tools {} due to spec setting'.format(
                        latest_v_build_tools))

        # 2. check aidl can be run
        self._check_aidl(installed_v_build_tools)

        # 3. finally, install the android for the current api
        self.logger.info('Downloading platform api target if necessary')
        android_platform = join(self.android_sdk_dir, 'platforms', 'android-{}'.format(self.android_api))
        if not buildops.file_exists(android_platform):
            if not skip_upd:
                self._sdkmanager(f"platforms;android-{self.android_api}")
            else:
                self.logger.info(
                    'Skipping install API {} platform tools due to spec setting'.format(
                        self.android_api))

        self.logger.info('Android packages installation done.')

        self.buildozer.state[cache_key] = cache_value
        self.buildozer.state.sync()

    def _check_aidl(self, v_build_tools):
        self.logger.debug('Check that aidl can be executed')
        v_build_tools = self._read_version_subdir(self.android_sdk_dir,
                                                  'build-tools')
        aidl_cmd = join(self.android_sdk_dir, 'build-tools',
                        str(v_build_tools), 'aidl')
        buildops.checkbin('Aidl', aidl_cmd)
        returncode = buildops.cmd(
            [aidl_cmd],
            break_on_error=False,
            show_output=False,
            env=self.buildozer.environ).return_code
        if returncode != 1:
            self.logger.error('Aidl cannot be executed')
            if architecture()[0] == '64bit':
                self.logger.error('')
                self.logger.error(
                    'You might have missed to install 32bits libs')
                self.logger.error(
                    'Check https://buildozer.readthedocs.org/en/latest/installation.html')
                self.logger.error('')
            else:
                self.logger.error('')
                self.logger.error(
                    'In case of a bug report, please add a full log with log_level = 2')
                self.logger.error('')
            raise BuildozerException()

    def install_platform(self):
        self._install_p4a()
        self._install_apache_ant()
        self._install_android_sdk()
        self._install_android_ndk()
        self._install_android_packages()

        # ultimate configuration check.
        # some of our configuration cannot be checked without platform.
        self.check_configuration_tokens()
        if not self._p4a_have_aab_support():
            self.logger.error(
                "This buildozer version requires a python-for-android version with AAB (Android App Bundle) support. "
                "Please update your pinned version accordingly."
            )
            raise BuildozerException()

        self.buildozer.environ.update({
            'PACKAGES_PATH': self.buildozer.global_packages_dir,
            'ANDROIDSDK': self.android_sdk_dir,
            'ANDROIDNDK': self.android_ndk_dir,
            'ANDROIDAPI': self.android_api,
            'ANDROIDMINAPI': self.android_minapi,
        })

    def _install_p4a(self):
        p4a_fork = self.buildozer.config.getdefault(
            'app', 'p4a.fork', self.p4a_fork
        )
        p4a_url = self.buildozer.config.getdefault(
            'app', 'p4a.url', f'https://github.com/{p4a_fork}/python-for-android.git'
        )
        p4a_branch = self.buildozer.config.getdefault(
            'app', 'p4a.branch', self.p4a_branch
        )
        p4a_commit = self.buildozer.config.getdefault(
            'app', 'p4a.commit', self.p4a_commit
        )

        p4a_dir = self.p4a_dir
        system_p4a_dir = self.buildozer.config.getdefault('app',
                                                          'p4a.source_dir')
        if system_p4a_dir:
            # Don't install anything, just check that the dir does exist
            if not buildops.file_exists(p4a_dir):
                self.logger.error(
                    'Path for p4a.source_dir does not exist')
                self.logger.error('')
                raise BuildozerException()
        else:
            # check that url/branch has not been changed
            if buildops.file_exists(p4a_dir):
                cur_url = buildops.cmd(
                    ["git", "config", "--get", "remote.origin.url"],
                    get_stdout=True,
                    cwd=p4a_dir,
                    env=self.buildozer.environ
                ).stdout.strip()
                cur_branch = buildops.cmd(
                    ["git", "branch", "-vv"],
                    get_stdout=True,
                    cwd=p4a_dir,
                    env=self.buildozer.environ
                ).stdout.split()[1]
                if any([cur_url != p4a_url, cur_branch != p4a_branch]):
                    self.logger.info(
                        f"Detected old url/branch ({cur_url}/{cur_branch}), deleting..."
                    )
                    buildops.rmdir(p4a_dir)

            if not buildops.file_exists(p4a_dir):
                buildops.cmd(
                    [
                        "git",
                        "clone",
                        "-b",
                        p4a_branch,
                        "--single-branch",
                        p4a_url,
                        self.p4a_directory_name,
                    ],
                    cwd=self.buildozer.platform_dir,
                    env=self.buildozer.environ
                )
            elif self.platform_update:
                buildops.cmd(
                    ["git", "clean", "-dxf"],
                    cwd=p4a_dir,
                    env=self.buildozer.environ)
                current_branch = buildops.cmd(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    get_stdout=True,
                    cwd=p4a_dir,
                    env=self.buildozer.environ).stdout.strip()
                if current_branch == p4a_branch:
                    buildops.cmd(
                        ["git", "pull"],
                        cwd=p4a_dir,
                        env=self.buildozer.environ)
                else:
                    buildops.cmd(
                        ["git", "fetch", "--tags", "origin", "{0}:{0}".format(p4a_branch)],
                        cwd=p4a_dir,
                        env=self.buildozer.environ)
                    buildops.cmd(
                        ["git", "checkout", p4a_branch],
                        cwd=p4a_dir,
                        env=self.buildozer.environ)
            if p4a_commit != 'HEAD':
                buildops.cmd(
                    ["git", "reset", "--hard", p4a_commit],
                    cwd=p4a_dir,
                    env=self.buildozer.environ)

        # also install dependencies (currently, only setup.py knows about it)
        # let's extract them.
        try:
            with open(join(self.p4a_dir, "setup.py")) as fd:
                setup = fd.read()
                deps = re.findall(r"^\s*install_reqs = (\[[^\]]*\])", setup, re.DOTALL | re.MULTILINE)[0]
                deps = ast.literal_eval(deps)
        except IOError:
            self.logger.error('Failed to read python-for-android setup.py at {}'.format(
                join(self.p4a_dir, 'setup.py')))
            sys.exit(1)

        # in virtualenv or conda env
        options = ["--user"]
        if "VIRTUAL_ENV" in os.environ or "CONDA_PREFIX" in os.environ:
            options = []
        buildops.cmd(
            [executable, "-m", "pip", "install", "-q", *options, *deps],
            env=self.buildozer.environ)

    def compile_platform(self):
        app_requirements = self.buildozer.config.getlist(
            'app', 'requirements', '')
        dist_name = self.buildozer.config.get('app', 'package.name')
        local_recipes = self.get_local_recipes_dir()
        requirements = ','.join(app_requirements)
        options = []

        source_dirs = {
            'P4A_{}_DIR'.format(name[20:]): realpath(expanduser(value))
            for name, value in self.buildozer.config.items('app')
            if name.startswith('requirements.source.')
        }
        if source_dirs:
            self.buildozer.environ.update(source_dirs)
            self.logger.info('Using custom source dirs:\n    {}'.format(
                '\n    '.join(['{} = {}'.format(k, v)
                               for k, v in source_dirs.items()])))

        if self.buildozer.config.getbooldefault('app', 'android.copy_libs', True):
            options.append("--copy-libs")
        # support for recipes in a local directory within the project
        if local_recipes:
            options.append('--local-recipes')
            options.append(local_recipes)

        p4a_create = ["create", f"--dist_name={dist_name}", f"--bootstrap={self._p4a_bootstrap}", f"--requirements={requirements}"]

        for arch in self._archs:
            p4a_create.append(f"--arch={arch}")

        p4a_create.extend(options)

        self._p4a(p4a_create, get_stdout=True, env=self.buildozer.environ)

    def get_available_packages(self):
        return True

    def get_dist_dir(self, dist_name):
        """Find the dist dir with the given name if one
        already exists, otherwise return a new dist_dir name.
        """

        # If the expected dist name does exist, simply use that
        expected_dist_dir = join(self._build_dir, 'dists', dist_name)
        if exists(expected_dist_dir):
            return expected_dist_dir

        # If no directory has been found yet, our dist probably
        # doesn't exist yet, so use the expected name
        return expected_dist_dir

    def get_local_recipes_dir(self):
        local_recipes = self.buildozer.config.getdefault('app', 'p4a.local_recipes')
        return realpath(expanduser(local_recipes)) if local_recipes else None

    def execute_build_package(self, build_cmd):
        # wrapper from previous old_toolchain to new toolchain
        dist_name = self.buildozer.config.get('app', 'package.name')
        local_recipes = self.get_local_recipes_dir()
        cmd = [self.artifact_format, "--bootstrap", self._p4a_bootstrap, "--dist_name", dist_name]
        for args in build_cmd:
            option, values = args[0], args[1:]
            if option == "debug":
                continue
            elif option == "release":
                cmd.append("--release")
                if self.check_p4a_sign_env(True):
                    cmd.append("--sign")
                continue
            if option == "--window":
                cmd.append("--window")
            elif option == "--sdk":
                cmd.append("--android_api")
                cmd.extend(values)
            else:
                cmd.extend(args)

        # support for presplash background color
        presplash_color = self.buildozer.config.getdefault('app', 'android.presplash_color', None)
        if presplash_color:
            cmd.append('--presplash-color')
            cmd.append("{}".format(presplash_color))

        # support for services
        services = self.buildozer.config.getlist('app', 'services', [])
        for service in services:
            cmd.append("--service")
            cmd.append(service)

        # support for copy-libs
        if self.buildozer.config.getbooldefault('app', 'android.copy_libs', True):
            cmd.append("--copy-libs")

        # Home-app usage
        if self.buildozer.config.getbooldefault('app', 'android.home_app', False):
            cmd.append("--home-app")

        # Enable display-cutout for Android devices
        display_cutout = self.buildozer.config.getdefault('app', 'android.display_cutout', 'never').lower()
        if display_cutout in {'default', 'shortedges'}:
            if display_cutout == 'shortedges':
                display_cutout = 'shortEdges'
            cmd.append("--display-cutout={}".format(display_cutout))

        # support for recipes in a local directory within the project
        if local_recipes:
            cmd.append('--local-recipes')
            cmd.append(local_recipes)

        # support for blacklist/whitelist filename
        whitelist_src = self.buildozer.config.getdefault('app', 'android.whitelist_src', None)
        blacklist_src = self.buildozer.config.getdefault('app', 'android.blacklist_src', None)
        if whitelist_src:
            cmd.append('--whitelist')
            cmd.append(realpath(expanduser(whitelist_src)))
        if blacklist_src:
            cmd.append('--blacklist')
            cmd.append(realpath(expanduser(blacklist_src)))

        # support for java directory
        javadirs = self.buildozer.config.getlist('app', 'android.add_src', [])
        for javadir in javadirs:
            cmd.append('--add-source')
            cmd.append(realpath(expanduser(javadir)))

        # support for aars
        aars = self.buildozer.config.getlist('app', 'android.add_aars', [])
        for aar in aars:
            cmd.append('--add-aar')
            cmd.append(realpath(expanduser(aar)))

        # support for assets folder
        assets = self.buildozer.config.getlist('app', 'android.add_assets', [])
        for asset in assets:
            cmd.append('--add-asset')
            if ':' in asset:
                asset_src, asset_dest = asset.split(":")
            else:
                asset_src = asset
                asset_dest = asset
            cmd.append(realpath(expanduser(asset_src)) + ':' + asset_dest)

        # support for res folder
        resources = self.buildozer.config.getlist('app', 'android.add_resources', [])
        for resource in resources:
            cmd.append('--add-resource')
            if ':' in resource:
                resource_src, resource_dest = resource.split(":")
            else:
                resource_src = resource
                resource_dest = ""
            cmd.append(realpath(expanduser(resource_src)) + ':' + resource_dest)

        # support for uses-lib
        uses_library = self.buildozer.config.getlist(
            'app', 'android.uses_library', '')
        for lib in uses_library:
            cmd.append('--uses-library={}'.format(lib))

        # support for activity-class-name
        activity_class_name = self.buildozer.config.getdefault(
            'app', 'android.activity_class_name', 'org.kivy.android.PythonActivity')
        if activity_class_name != 'org.kivy.android.PythonActivity':
            cmd.append('--activity-class-name={}'.format(activity_class_name))

        # support for service-class-name
        service_class_name = self.buildozer.config.getdefault(
            'app', 'android.service_class_name', 'org.kivy.android.PythonService')
        if service_class_name != 'org.kivy.android.PythonService':
            cmd.append('--service-class-name={}'.format(service_class_name))

        # support for extra-manifest-xml
        extra_manifest_xml = self.buildozer.config.getdefault(
            'app', 'android.extra_manifest_xml', '')
        if extra_manifest_xml:
            cmd.append('--extra-manifest-xml')
            cmd.append('{}'.format(open(extra_manifest_xml, 'rt').read()))

        # support for extra-manifest-application-arguments
        extra_manifest_application_arguments = self.buildozer.config.getdefault(
            'app', 'android.extra_manifest_application_arguments', '')
        if extra_manifest_application_arguments:
            cmd.append('--extra-manifest-application-arguments')
            cmd.append('{}'.format(open(extra_manifest_application_arguments, 'rt').read()))

        # support for gradle dependencies
        gradle_dependencies = self.buildozer.config.getlist('app', 'android.gradle_dependencies', [])
        for gradle_dependency in gradle_dependencies:
            cmd.append('--depend')
            cmd.append(gradle_dependency)

        # support for manifestPlaceholders
        manifest_placeholders = self.buildozer.config.getdefault('app', 'android.manifest_placeholders', None)
        if manifest_placeholders:
            cmd.append('--manifest-placeholders')
            cmd.append("{}".format(manifest_placeholders))

        # support disabling of byte compile for .py files
        no_byte_compile = self.buildozer.config.getdefault('app', 'android.no-byte-compile-python', False)
        if no_byte_compile:
            cmd.append('--no-byte-compile-python')

        for arch in self._archs:
            cmd.append('--arch')
            cmd.append(arch)

        self._p4a(cmd, env=self.buildozer.environ)

    def get_release_mode(self):
        # aab, also if unsigned is named as *-release
        if self.check_p4a_sign_env() or self.artifact_format in ["aab", "aar"]:
            return "release"
        return "release-unsigned"

    def check_p4a_sign_env(self, error=False):
        keys = ["KEYALIAS", "KEYSTORE_PASSWD", "KEYSTORE", "KEYALIAS_PASSWD"]
        check = True
        for key in keys:
            key = "P4A_RELEASE_{}".format(key)
            if key not in os.environ:
                if error:
                    self.logger.error(
                        ("Asking for release but {} is missing"
                         "--sign will not be passed").format(key))
                check = False
        return check

    def cmd_debug(self, *args):
        self.artifact_format = self.buildozer.config.getdefault('app', 'android.debug_artifact', 'apk')
        super().cmd_debug(*args)

    def cmd_release(self, *args):
        self.artifact_format = self.buildozer.config.getdefault('app', 'android.release_artifact', 'aab')
        super().cmd_release(*args)

    def cmd_run(self, *args):
        entrypoint = self.buildozer.config.getdefault(
            'app', 'android.entrypoint')
        if not entrypoint:
            self.buildozer.config.set('app', 'android.entrypoint', 'org.kivy.android.PythonActivity')

        super().cmd_run(*args)

        entrypoint = self.buildozer.config.getdefault(
            'app', 'android.entrypoint', 'org.kivy.android.PythonActivity')

        package = self._get_package()

        # push on the device
        for serial in self.serials:
            self.buildozer.environ['ANDROID_SERIAL'] = serial
            self.logger.info('Run on {}'.format(serial))
            buildops.cmd(
                [
                    self.adb_executable,
                    *self.adb_args,
                    "shell",
                    "am",
                    "start",
                    "-n",
                    f"{package}/{entrypoint}",
                    "-a",
                    entrypoint,
                ],
                cwd=self.buildozer.global_platform_dir,
                env=self.buildozer.environ
            )
        self.buildozer.environ.pop('ANDROID_SERIAL', None)

        while True:
            if self._get_pid():
                break
            sleep(.1)
            self.logger.info('Waiting for application to start.')

        self.logger.info('Application started.')

    def cmd_p4a(self, *args):
        '''
        Run p4a commands. Args must come after --, or
        use --alias to make an alias
        '''
        self.check_requirements()
        self.install_platform()
        args = args[0]
        if args and args[0] == '--alias':
            print('To set up p4a in this shell session, execute:')
            print('    alias p4a=$(buildozer {} p4a --alias 2>&1 >/dev/null)'
                  .format(self.targetname))
            sys.stderr.write('PYTHONPATH={} {}\n'.format(self.p4a_dir, self._p4a_cmd))
        else:
            self._p4a(args, env=self.buildozer.environ)

    def cmd_clean(self, *args):
        '''
        Clean the build and distribution
        '''
        self._p4a(["clean_builds"], env=self.buildozer.environ)
        self._p4a(["clean_dists"], env=self.buildozer.environ)

    def _get_package(self):
        config = self.buildozer.config
        package_domain = config.getdefault('app', 'package.domain', '')
        package = config.get('app', 'package.name')
        if package_domain:
            package = package_domain + '.' + package
        return package.lower()

    def _generate_whitelist(self, dist_dir):
        p4a_whitelist = self.buildozer.config.getlist(
            'app', 'android.whitelist') or []
        whitelist_fn = join(dist_dir, 'whitelist.txt')
        with open(whitelist_fn, 'w') as fd:
            for wl in p4a_whitelist:
                fd.write(wl + '\n')

    def build_package(self):
        dist_name = self.buildozer.config.get('app', 'package.name')
        dist_dir = self.get_dist_dir(dist_name)
        config = self.buildozer.config
        package = self._get_package()
        version = self.buildozer.get_version()

        # add extra libs/armeabi files in dist/default/libs/armeabi
        # (same for armeabi-v7a, arm64-v8a, x86, mips)
        for config_key, lib_dir in (
                ('android.add_libs_armeabi', 'armeabi'),
                ('android.add_libs_armeabi_v7a', 'armeabi-v7a'),
                ('android.add_libs_arm64_v8a', 'arm64-v8a'),
                ('android.add_libs_x86', 'x86'),
                ('android.add_libs_mips', 'mips')):

            patterns = config.getlist('app', config_key, [])
            if not patterns:
                continue
            if lib_dir not in self._archs:
                continue

            self.logger.debug('Search and copy libs for {}'.format(lib_dir))
            for fn in buildops.file_matches(patterns):
                buildops.file_copy(
                    join(self.buildozer.root_dir, fn),
                    join(dist_dir, 'libs', lib_dir, basename(fn)))

        # update the project.properties libraries references
        self._update_libraries_references(dist_dir)

        # generate the whitelist if needed
        self._generate_whitelist(dist_dir)

        # build the app
        build_cmd = [
            ("--name", config.get('app', 'title')),
            ("--version", version),
            ("--package", package),
            ("--minsdk", config.getdefault('app', 'android.minapi',
                                           self.android_minapi)),
            ("--ndk-api", config.getdefault('app', 'android.minapi',
                                            self.android_minapi)),
            ("--private", self.buildozer.app_dir),
        ]

        # add permissions
        permissions = config.getlist('app', 'android.permissions', [])
        for permission in permissions:
            build_cmd += [("--permission", permission)]

        # add features
        features = config.getlist('app', 'android.features', [])
        for feature in features:
            build_cmd += [("--feature", feature)]

        # add res_xml
        xmlfiles = config.getlist('app', 'android.res_xml', [])
        for xmlfile in xmlfiles:
            build_cmd += [("--res_xml", join(self.buildozer.root_dir,
                                                    xmlfile))]

        # android.entrypoint
        entrypoint = config.getdefault('app', 'android.entrypoint', 'org.kivy.android.PythonActivity')
        build_cmd += [('--android-entrypoint', entrypoint)]

        # android.apptheme
        apptheme = config.getdefault('app', 'android.apptheme', '@android:style/Theme.NoTitleBar')
        build_cmd += [('--android-apptheme', apptheme)]

        # android.compile_options
        compile_options = config.getlist('app', 'android.add_compile_options', [])
        for option in compile_options:
            build_cmd += [('--add-compile-option', option)]

        # android.add_gradle_repositories
        repos = config.getlist('app', 'android.add_gradle_repositories', [])
        for repo in repos:
            build_cmd += [('--add-gradle-repository', repo)]

        # android packaging options
        pkgoptions = config.getlist('app', 'android.add_packaging_options', [])
        for pkgoption in pkgoptions:
            build_cmd += [('--add-packaging-option', pkgoption)]

        # meta-data
        meta_datas = config.getlistvalues('app', 'android.meta_data', [])
        for meta in meta_datas:
            key, value = meta.split('=', 1)
            meta = '{}={}'.format(key.strip(), value.strip())
            build_cmd += [("--meta-data", meta)]

        # add extra Java jar files
        add_jars = config.getlist('app', 'android.add_jars', [])
        for pattern in add_jars:
            pattern = join(self.buildozer.root_dir, pattern)
            matches = glob(expanduser(pattern.strip()))
            if matches:
                for jar in matches:
                    build_cmd += [("--add-jar", jar)]
            else:
                raise SystemError('Failed to find jar file: {}'.format(
                    pattern))

        # add Java activity
        add_activities = config.getlist('app', 'android.add_activities', [])
        for activity in add_activities:
            build_cmd += [("--add-activity", activity)]

        # add presplash, lottie animation or static
        presplash = config.getdefault('app', 'android.presplash_lottie', '')
        if presplash:
            build_cmd += [("--presplash-lottie", join(self.buildozer.root_dir,
                                                      presplash))]
        else:
            presplash = config.getdefault('app', 'presplash.filename', '')
            if presplash:
                build_cmd += [("--presplash", join(self.buildozer.root_dir,
                                                   presplash))]

        # add icon
        icon = config.getdefault('app', 'icon.filename', '')
        if icon:
            build_cmd += [("--icon", join(self.buildozer.root_dir, icon))]
        icon_fg = config.getdefault('app', 'icon.adaptive_foreground.filename', '')
        icon_bg = config.getdefault('app', 'icon.adaptive_background.filename', '')
        if icon_fg and icon_bg:
            build_cmd += [("--icon-fg", join(self.buildozer.root_dir, icon_fg))]
            build_cmd += [("--icon-bg", join(self.buildozer.root_dir, icon_bg))]

        # OUYA Console support
        ouya_category = config.getdefault('app', 'android.ouya.category',
                                          '').upper()
        if ouya_category:
            if ouya_category not in ('GAME', 'APP'):
                raise SystemError(
                    'Invalid android.ouya.category: "{}" must be one of GAME or APP'.format(
                        ouya_category))
            # add icon
            ouya_icon = config.getdefault('app', 'android.ouya.icon.filename',
                                          '')
            build_cmd += [("--ouya-category", ouya_category)]
            build_cmd += [("--ouya-icon", join(self.buildozer.root_dir,
                                               ouya_icon))]

        if config.getdefault('app', 'p4a.bootstrap', 'sdl2') != 'service_only':
            # add orientation
            orientation = config.getlist('app', 'orientation', ['landscape'])
            for orient in orientation:
                build_cmd += [("--orientation", orient)]

            # fullscreen ?
            fullscreen = config.getbooldefault('app', 'fullscreen', True)
            if not fullscreen:
                build_cmd += [("--window", )]

        # wakelock ?
        wakelock = config.getbooldefault('app', 'android.wakelock', False)
        if wakelock:
            build_cmd += [("--wakelock", )]

        # AndroidX ?
        enable_androidx = config.getbooldefault('app',
                                                'android.enable_androidx',
                                                self.android_api > "28")
        if enable_androidx:
            build_cmd += [("--enable-androidx", )]

        # intent filters
        intent_filters = config.getdefault(
            'app', 'android.manifest.intent_filters', '')
        if intent_filters:
            build_cmd += [("--intent-filters", join(self.buildozer.root_dir,
                                                    intent_filters))]

        # activity launch mode
        launch_mode = config.getdefault(
            'app', 'android.manifest.launch_mode', '')
        if launch_mode:
            build_cmd += [("--activity-launch-mode", launch_mode)]

        # screenOrientation
        manifest_orientation = config.getdefault(
            'app', 'android.manifest.orientation', '')
        if manifest_orientation:
            build_cmd += [("--manifest-orientation", manifest_orientation)]

        # numeric version
        numeric_version = config.getdefault('app', 'android.numeric_version')
        if numeric_version:
            build_cmd += [("--numeric-version", numeric_version)]

        # android.allow_backup
        allow_backup = config.getbooldefault('app', 'android.allow_backup', True)
        if not allow_backup:
            build_cmd += [('--allow-backup', 'false')]

        # android.backup_rules
        backup_rules = config.getdefault('app', 'android.backup_rules', '')
        if backup_rules:
            build_cmd += [("--backup-rules", join(self.buildozer.root_dir,
                                                  backup_rules))]

        # build only in debug right now.
        if self.build_mode == 'debug':
            build_cmd += [("debug", )]
            mode = 'debug'
            mode_sign = mode
        else:
            build_cmd += [("release", )]
            mode_sign = "release"
            mode = self.get_release_mode()

        self.execute_build_package(build_cmd)

        try:
            self.buildozer.hook("android_pre_build_apk")
            self.execute_build_package(build_cmd)
            self.buildozer.hook("android_post_build_apk")
        except:
            # maybe the hook fail because the apk is not
            pass

        build_tools_versions = os.listdir(join(self.android_sdk_dir, "build-tools"))
        build_tools_versions = sorted(
            build_tools_versions, key=packaging.version.parse
        )
        build_tools_version = build_tools_versions[-1]
        gradle_files = ["build.gradle", "gradle", "gradlew"]
        is_gradle_build = build_tools_version >= "25.0" and any(
            (exists(join(dist_dir, x)) for x in gradle_files))
        packagename = config.get('app', 'package.name')

        if is_gradle_build:
            # on gradle build, the apk use the package name, and have no version
            packagename_src = basename(dist_dir)  # gradle specifically uses the folder name
            artifact = u'{packagename}-{mode}.{artifact_format}'.format(
                packagename=packagename_src, mode=mode, artifact_format=self.artifact_format)
            if self.artifact_format == "apk":
                artifact_dir = join(dist_dir, "build", "outputs", "apk", mode_sign)
            elif self.artifact_format == "aab":
                artifact_dir = join(dist_dir, "build", "outputs", "bundle", mode_sign)
            elif self.artifact_format == "aar":
                artifact_dir = join(dist_dir, "build", "outputs", "aar")

        else:
            # on ant, the apk use the title, and have version
            bl = u'\'" ,'
            apptitle = config.get('app', 'title')
            if hasattr(apptitle, 'decode'):
                apptitle = apptitle.decode('utf-8')
            apktitle = ''.join([x for x in apptitle if x not in bl])
            artifact = u'{title}-{version}-{mode}.apk'.format(
                title=apktitle,
                version=version,
                mode=mode)
            artifact_dir = join(dist_dir, "bin")

        artifact_dest = u'{packagename}-{version}-{arch}-{mode}.{artifact_format}'.format(
            packagename=packagename, mode=mode, version=version,
            arch=self.archs_snake, artifact_format=self.artifact_format)

        # copy to our place
        buildops.file_copy(
            join(artifact_dir, artifact),
            join(self.buildozer.bin_dir, artifact_dest))

        self.logger.info('Android packaging done!')
        self.logger.info(
            u'APK {0} available in the bin directory'.format(artifact_dest))
        self.buildozer.state['android:latestapk'] = artifact_dest
        self.buildozer.state['android:latestmode'] = self.build_mode

    def _update_libraries_references(self, dist_dir):
        # ensure the project.properties exist
        project_fn = join(dist_dir, 'project.properties')

        if not buildops.file_exists(project_fn):
            content = [
                'target=android-{}\n'.format(self.android_api),
                'APP_PLATFORM={}\n'.format(self.android_minapi)]
        else:
            with io.open(project_fn, encoding='utf-8') as fd:
                content = fd.readlines()

        # extract library reference
        references = []
        for line in content[:]:
            if not line.startswith('android.library.reference.'):
                continue
            content.remove(line)

        # convert our references to relative path
        app_references = self.buildozer.config.getlist(
            'app', 'android.library_references', [])
        source_dir = realpath(expanduser(self.buildozer.config.getdefault(
            'app', 'source.dir', '.')))
        for cref in app_references:
            # get the full path of the current reference
            ref = realpath(join(source_dir, cref))
            if not buildops.file_exists(ref):
                self.logger.error(
                    'Invalid library reference (path not found): {}'.format(
                        cref))
                sys.exit(1)
            # get a relative path from the project file
            ref = relpath(ref, realpath(expanduser(dist_dir)))
            # ensure the reference exists
            references.append(ref)

        # recreate the project.properties
        with io.open(project_fn, 'w', encoding='utf-8') as fd:

            try:
                fd.writelines((line.decode('utf-8') for line in content))
            except:
                fd.writelines(content)
            if content and not content[-1].endswith(u'\n'):
                fd.write(u'\n')
            for index, ref in enumerate(references):
                fd.write(u'android.library.reference.{}={}\n'.format(index + 1, ref))

        self.logger.debug('project.properties updated')

    @property
    def serials(self):
        if self._serials is not None:
            return self._serials
        serial = environ.get('ANDROID_SERIAL')
        if serial:
            return serial.split(',')
        lines = buildops.cmd(
            [self.adb_executable, *self.adb_args, "devices"],
            get_stdout=True,
            env=self.buildozer.environ
        ).stdout.splitlines()
        serials = []
        for serial in lines:
            if not serial:
                continue
            if serial.startswith('*') or serial.startswith('List '):
                continue
            serials.append(serial.split()[0])
        self._serials = serials
        return serials

    def cmd_adb(self, *args):
        '''
        Run adb from the Android SDK.
        Args must come after --, or use
        --alias to make an alias
        '''
        self.check_requirements()
        self.install_platform()
        args = args[0]
        if args and args[0] == '--alias':
            print('To set up ADB in this shell session, execute:')
            print('    alias adb=$(buildozer {} adb --alias 2>&1 >/dev/null)'
                  .format(self.targetname))
            sys.stderr.write(self.adb_executable + '\n')
        else:
            buildops.cmd(
                [self.adb_executable, *self.adb_args, *args],
                env=self.buildozer.environ)

    def cmd_deploy(self, *args):
        super().cmd_deploy(*args)
        state = self.buildozer.state
        if 'android:latestapk' not in state:
            self.logger.error('No APK built yet. Run "debug" first.')

        if state.get('android:latestmode', '') != 'debug':
            self.logger.error('Only debug APK are supported for deploy')
            return

        # search the APK in the bin dir
        apk = state['android:latestapk']
        full_apk = join(self.buildozer.bin_dir, apk)
        if not buildops.file_exists(full_apk):
            self.logger.error(
                'Unable to found the latest APK. Please run "debug" again.')

        # push on the device
        for serial in self.serials:
            self.buildozer.environ['ANDROID_SERIAL'] = serial
            self.logger.info('Deploy on {}'.format(serial))
            buildops.cmd(
                [self.adb_executable, *self.adb_args, "install", "-r", full_apk],
                cwd=self.buildozer.global_platform_dir,
                env=self.buildozer.environ
            )
        self.buildozer.environ.pop('ANDROID_SERIAL', None)

        self.logger.info('Application pushed.')

    def _get_pid(self):
        pid = buildops.cmd(
            [
                self.adb_executable,
                *self.adb_args,
                "shell",
                "pidof",
                self._get_package(),
            ],
            get_stdout=True,
            show_output=False,
            break_on_error=False,
            quiet=True,
            env=self.buildozer.environ
        ).stdout
        if pid:
            return pid.strip()
        return False

    def cmd_logcat(self, *args):
        '''Show the log from the device
        '''
        self.check_requirements()
        serial = self.serials[0:]
        if not serial:
            return
        filters = self.buildozer.config.getlist(
            "app",
            "android.logcat_filters",
            default="",
            section_sep=":",
            strip=False)
        filters = " ".join(filters)
        self.buildozer.environ['ANDROID_SERIAL'] = serial[0]
        extra_args = []
        pid = None
        if self.buildozer.config.getdefault('app', 'android.logcat_pid_only'):
            pid = self._get_pid()
            if pid:
                extra_args.extend(('--pid', pid))

        buildops.cmd(
            [self.adb_executable, *self.adb_args, "logcat", filters, *extra_args],
            cwd=self.buildozer.global_platform_dir,
            show_output=True,
            run_condition=self._get_pid if pid else None,
            break_on_error=False,
            env=self.buildozer.environ
        )

        self.logger.info(f"{self._get_package()} terminated")

        self.buildozer.environ.pop('ANDROID_SERIAL', None)


def get_target(buildozer):
    buildozer.targetname = "android"
    return TargetAndroid(buildozer)
