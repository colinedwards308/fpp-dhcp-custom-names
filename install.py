#!/usr/bin/env python3
"""Install persistent DHCP Custom Names on FPP via SSH or locally.

Mac:    python3 install.py --check
        python3 install.py
Player: sudo python3 install.py --local

Keep install.py, custom-names.patch, and dhcp-custom-names.php together.
Requires Python 3.8+, OpenSSH on the Mac, and PHP/git on FPP.
"""
import argparse
import getpass
import ipaddress
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile



# Edit these defaults, or override the address/user on the command line.
FPP_IP_ADDRESS = "10.10.11.1"
FPP_USER_NAME = ""       # Example: "fpp"
FPP_USER_PASSWORD = ""   # Example: "falcon"

PACKAGE_FILES = ('install.py', 'custom-names.patch', 'dhcp-custom-names.php')
FILES = ('www/proxies.php', 'www/api/controllers/proxies.php', 'www/api/index.php')


def run(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        output = result.stdout.decode('utf-8', errors='replace')
        raise RuntimeError('{} failed:\n{}'.format(shlex.join(command), output))
    return result.stdout


def patch_check(stage, patch, reverse=False):
    command = ['git', 'apply', '--check', '--whitespace=nowarn']
    if reverse:
        command.append('--reverse')
    result = subprocess.run(command + ['-'], input=patch, cwd=str(stage),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode == 0, result.stdout.decode('utf-8', errors='replace')


def atomic_write(path, content, info):
    fd, temporary = tempfile.mkstemp(prefix='.dhcp-names-install-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.chmod(temporary, info.st_mode & 0o7777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(args):
    root = Path(args.root).resolve()
    media = Path(args.media_dir).resolve()
    for executable in ('git', 'php'):
        if not shutil.which(executable):
            raise RuntimeError('{} is required on the FPP player.'.format(executable))
    originals, metadata = {}, {}
    for relative in FILES:
        path = root / relative
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise RuntimeError('Expected a regular FPP source file: {}'.format(path))
        originals[relative] = path.read_bytes()
        metadata[relative] = path.stat()
    patch = (Path(__file__).resolve().parent / 'custom-names.patch').read_bytes()
    with tempfile.TemporaryDirectory(prefix='fpp-dhcp-names-') as work:
        stage = Path(work)
        # An empty repository keeps git apply independent of any enclosing checkout.
        run(['git', 'init', '-q', str(stage)])
        for relative, content in originals.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        applied, _ = patch_check(stage, patch, reverse=True)
        if applied:
            print('Custom Names is already installed; no files changed.')
            return
        compatible, detail = patch_check(stage, patch)
        if not compatible:
            raise RuntimeError('This FPP version is incompatible or partially patched. '
                               'No live files were changed. The installer needs adapting to this version.\n' + detail)
        run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=str(stage), input=patch)
        for relative in FILES:
            run(['php', '-d', 'short_open_tag=1', '-l', str(stage / relative)])
        test = stage / 'tests' / 'dhcp-custom-names.php'
        test.parent.mkdir()
        test.write_bytes((Path(__file__).resolve().parent / 'dhcp-custom-names.php').read_bytes())
        print(run(['php', '-d', 'short_open_tag=1', str(test)]).decode(), end='')
        if args.check:
            print('Compatible: patch and PHP regression checks passed. No live files changed.')
            return
        if os.geteuid() != 0:
            raise RuntimeError('Installation requires root. Use sudo with --local, or passwordless sudo over SSH.')
        # Serialize installers. The lock is in the root-owned source tree.
        import fcntl
        lock_path = root / '.dhcp-custom-names-install.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for relative, content in originals.items():
                if (root / relative).read_bytes() != content:
                    raise RuntimeError('FPP files changed during checks. No files installed; retry after the upgrade finishes.')
            backups = media / 'backups'
            backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            backup = backups / ('dhcp-custom-names-before-' + stamp + '.tar.gz')
            manifest = {'created_utc': stamp, 'root': str(root), 'sha256': {
                key: hashlib.sha256(value).hexdigest() for key, value in originals.items()}}
            with tarfile.open(str(backup), 'x:gz') as archive:
                for relative in FILES:
                    archive.add(str(root / relative), arcname=relative, recursive=False)
                names = media / 'config' / 'dhcp-proxy-names.json'
                if names.is_file():
                    archive.add(str(names), arcname='saved-names.json', recursive=False)
                payload = json.dumps(manifest, indent=2).encode()
                entry = tarfile.TarInfo('manifest.json')
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
            print('Backup: {}'.format(backup), flush=True)
            changed = []
            try:
                for relative in FILES:
                    changed.append(relative)
                    atomic_write(root / relative, (stage / relative).read_bytes(), metadata[relative])
                for relative in FILES:
                    if (root / relative).read_bytes() != (stage / relative).read_bytes():
                        raise RuntimeError('Installed file verification failed: ' + relative)
            except BaseException:
                failures = []
                for relative in reversed(changed):
                    try:
                        atomic_write(root / relative, originals[relative], metadata[relative])
                    except Exception as error:
                        failures.append(str(error))
                if failures:
                    print('Automatic rollback incomplete: ' + '; '.join(failures), file=sys.stderr)
                    print('Restore source files from ' + str(backup), file=sys.stderr)
                else:
                    print('Installation interrupted; original source files restored.', file=sys.stderr)
                raise
    print('Installed. Refresh Proxy Settings, enter Custom Names, and click Save Names.')
    print('Existing saved names were preserved. No FPP restart is required.')
    print('Rollback on the player:\n  sudo tar -xzf {} -C {} www'.format(
        shlex.quote(str(backup)), shlex.quote(str(root))))


def connection_target(args):
    if args.host:
        if args.ip_address or args.ip or args.user != FPP_USER_NAME:
            raise ValueError('Use --host alone, or use an IP address with --user.')
        target = args.host
        if target.startswith('-') or any(c.isspace() for c in target):
            raise ValueError('Invalid SSH target')
    else:
        address = args.ip_address or args.ip or FPP_IP_ADDRESS
        if args.ip_address and args.ip:
            raise ValueError('Supply either a positional IP address or --ip, not both.')
        ipaddress.ip_address(address)
        if not args.user or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in args.user) or args.user.startswith('-'):
            raise ValueError('Invalid SSH user name')
        target = args.user + '@' + address
    if not 1 <= args.port <= 65535:
        raise ValueError('Invalid SSH port')
    return target


def package_bytes():
    """Send three readable files, with no encoded code embedded in the installer."""
    package = Path(__file__).resolve().parent
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as archive:
        for name in PACKAGE_FILES:
            path = package / name
            if not path.is_file():
                raise RuntimeError('Missing {}. Keep all installer files in the same folder.'.format(name))
            archive.add(str(path), arcname=name, recursive=False)
    return payload.getvalue()


def install_remote(args):
    if not args.host and not args.user:
        args.user = input('FPP SSH user name: ').strip()
    target = connection_target(args)
    payload = package_bytes()
    password = getpass.getpass('FPP SSH password: ') if args.ask_password or not FPP_USER_PASSWORD else FPP_USER_PASSWORD
    command = ['ssh', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new',
               '-o', 'NumberOfPasswordPrompts=1', '-p', str(args.port)]
    if args.identity:
        command += ['-i', str(Path(args.identity).expanduser())]
    if args.known_hosts:
        command += ['-o', 'UserKnownHostsFile=' + str(Path(args.known_hosts).expanduser())]
    worker = ([] if args.check else ['sudo', '-n']) + ['python3', 'install.py', '--local',
              '--root', args.root, '--media-dir', args.media_dir]
    if args.check:
        worker.append('--check')
    # Only the three explicitly named package files are sent to the player.
    remote = """set -eu
stage=$(mktemp -d /tmp/fpp-dhcp-names-package.XXXXXX)
trap 'rm -rf "$stage"' EXIT
 tar -xf - -C "$stage"
cd "$stage"
""" + shlex.join(worker)
    command += [target, 'bash -c ' + shlex.quote(remote)]
    print(('Checking ' if args.check else 'Installing on ') + target + '...', flush=True)
    with tempfile.TemporaryDirectory(prefix='fpp-ssh-') as temporary:
        helper = Path(temporary) / 'askpass.sh'
        # The helper contains no password. SSH receives it through a private
        # environment variable, never through command-line arguments or logs.
        helper.write_text("""#!/bin/sh
case "$1" in
  *assword*) printf '%s\n' "$FPP_INSTALL_SSH_PASSWORD" ;;
  *) exit 1 ;;
esac
""")
        helper.chmod(0o700)
        environment = os.environ.copy()
        environment.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force',
                           DISPLAY=environment.get('DISPLAY') or ':0',
                           FPP_INSTALL_SSH_PASSWORD=password)
        result = subprocess.run(command, input=payload, env=environment, start_new_session=True)
    if result.returncode == 255:
        print('SSH connection failed. Check the address, SSH access, and password. '
              'Use --ask-password if it differs from the configured default.', file=sys.stderr)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('ip_address', nargs='?', help='FPP IP address (default: FPP_IP_ADDRESS)')
    parser.add_argument('--ip', help='FPP IP address; alternative to the positional address')
    parser.add_argument('--user', default=FPP_USER_NAME, help='SSH user (default: %(default)s)')
    parser.add_argument('--host', help='Legacy SSH target, e.g. fpp@10.10.11.1')
    parser.add_argument('--ask-password', action='store_true', help='Prompt for a password instead of FPP_USER_PASSWORD')
    parser.add_argument('--port', type=int, default=22, help='SSH port')
    parser.add_argument('--identity', help='Optional SSH private key path')
    parser.add_argument('--known-hosts', help='Optional OpenSSH known_hosts file')
    parser.add_argument('--check', action='store_true', help='Check compatibility without modifying live files')
    parser.add_argument('--local', action='store_true', help='Run on the FPP player directly')
    parser.add_argument('--root', default='/opt/fpp', help='FPP source directory on the player')
    parser.add_argument('--media-dir', default='/home/fpp/media', help='FPP media directory on the player')
    args = parser.parse_args()
    try:
        if args.local:
            install(args)
        else:
            return install_remote(args)
    except (RuntimeError, OSError, ValueError, EOFError, subprocess.SubprocessError) as error:
        print('ERROR: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
