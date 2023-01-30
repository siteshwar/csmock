# Copyright (C) 2023 Red Hat, Inc.
#
# This file is part of csmock.
#
# csmock is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# csmock is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with csmock.  If not, see <http://www.gnu.org/licenses/>.

import os


# default URL to download snyk binary executable
SNYK_BIN_URL = "https://static.snyk.io/cli/latest/snyk-linux"

# default directory where downloaded snyk executable is cached across runs
SNYK_CACHE_DIR = "/var/tmp/csmock/snyk"

SNYK_SCAN_DIR = "/builddir/build/BUILD"

SNYK_OUTPUT = "/builddir/snyk-capture.sarif"

SNYK_LOG = "/builddir/snyk-capture.log"

FILTER_CMD = f"csgrep '%s' --mode=json --prepend-path-prefix={SNYK_SCAN_DIR}/ > '%s'"


class PluginProps:
    def __init__(self):
        self.description = "Tool to find vulnerabilities in source code."


class Plugin:
    def __init__(self):
        self.enabled = False
        self.auth_token_src = None
        self.snyk_bin = None

    def get_props(self):
        return PluginProps()

    def enable(self):
        self.enabled = True

    def init_parser(self, parser):
        parser.add_argument(
            "--snyk-bin-url", default=SNYK_BIN_URL,
            help="URL to download snyk binary executable")

        parser.add_argument(
            "--snyk-auth", default="~/.config/configstore/snyk.json",
            help="file containing snyk authentication token")

        parser.add_argument(
            "--snyk-cache-dir", default=SNYK_CACHE_DIR,
            help="directory where downloaded snyk tarballs are cached across runs")

        parser.add_argument(
            "--snyk-refresh", action="store_true",
            help="force download of snyk binary executable")

    def handle_args(self, parser, args, props):
        if not self.enabled:
            return

        # check whether we have access to snyk authentication token
        self.auth_token_src = os.path.expanduser(args.snyk_auth)
        if not os.access(self.auth_token_src, os.R_OK):
            results.error("unable to read snyk authentication token: %s" % self.auth_token_src)

        # fetch snyk using the given URL
        def fetch_snyk_hook(results):
            cache_dir = args.snyk_cache_dir
            try:
                # make sure the cache directory exists
                os.makedirs(cache_dir, mode=0o755, exist_ok=True)
            except OSError:
                results.error("failed to create snyk cache directory: %s" % cache_dir)
                return 1

            url = args.snyk_bin_url
            snyk_bin_name = url.split("/")[-1]
            self.snyk_bin = os.path.join(cache_dir, snyk_bin_name)

            if not args.snyk_refresh and os.path.exists(self.snyk_bin):
                results.print_with_ts("reusing previously downloaded snyk executable: %s" % self.snyk_bin)
            else:
                # fetch the binary executable
                ec = results.exec_cmd(['curl', '-Lfsvo', self.snyk_bin, url])
                if 0 != ec:
                    results.error("failed to download snyk binary executable: %s" % url)
                    return ec

                # add eXecute permission on the downloaded file
                os.chmod(self.snyk_bin, 0o755)

            # check whether we have eXecute access
            if not os.access(self.snyk_bin, os.X_OK):
                results.error("snyk binary is not executable: %s" % self.snyk_bin)
                return 2

            # query version of snyk
            (ec, out) = results.get_cmd_output([self.snyk_bin, 'version'], shell=False)
            if 0 != ec:
                return ec

            # parse and record the version of snyk
            ver = out.split(" ")[0]
            results.ini_writer.append("analyzer-version-snyk", ver)

            # copy snyk binary into the chroot
            props.copy_in_files += [self.snyk_bin]

            # get the results out of the chroot
            props.copy_out_files += [SNYK_OUTPUT, SNYK_LOG]
            return 0

        # fetch snyk binary executable before initializing the buildroot
        props.pre_mock_hooks += [fetch_snyk_hook]

        # make networking work in the chroot
        def copy_resolv_conf(results, mock):
            mock.copy_in_resolv_conf()
            return 0
        props.post_depinst_hooks += [copy_resolv_conf]

        def scan_hook(results, mock, props):
            # copy snyk authentication token into the chroot
            dst_dir = "/builddir/.config/configstore"
            mock.exec_chroot_cmd("mkdir -p %s" % dst_dir)
            auth_token_dst = os.path.join(dst_dir, "snyk.json")
            ec = mock.exec_mock_cmd(["--copyin", self.auth_token_src, auth_token_dst])
            if 0 != ec:
                results.error("failed to copy snyk authentication token", ec=ec)
                return ec

            # run snyk code
            cmd = "%s code test -d %s --sarif-file-output=%s >/dev/null 2>%s" \
                    % (self.snyk_bin, SNYK_SCAN_DIR, SNYK_OUTPUT, SNYK_LOG)
            ec = mock.exec_chroot_cmd(cmd)

            # remove authentication token from the chroot
            mock.exec_chroot_cmd("/bin/rm -fv %s" % auth_token_dst)

            # check exit code of snyk code itself
            if ec not in [0, 1]:
                results.error("snyk code returned unexpected exit status: %d" % ec, ec=ec)

            # returning non-zero would prevent csmock from archiving SNYK_LOG
            return 0

        # run snyk after successful build
        props.post_install_hooks += [scan_hook]

        # convert the results into the csdiff's JSON format
        def filter_hook(results):
            src = results.dbgdir_raw + SNYK_OUTPUT
            dst = "%s/snyk-capture.js" % results.dbgdir_uni
            cmd = FILTER_CMD % (src, dst)
            return results.exec_cmd(cmd, shell=True)
        props.post_process_hooks += [filter_hook]