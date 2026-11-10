"""Non-blocking, process-owned Android validation lease shared by local tools."""
import atexit
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess

PREFIX = 'SCENEVOX_ANDROID_VALIDATION_'


class Busy(RuntimeError):
    pass


def ancestor(pid):
    current = os.getpid()
    for _ in range(32):
        if current == pid:
            return True
        if current <= 1:
            break
        value = subprocess.check_output(['ps', '-o', 'ppid=', '-p', str(current)], text=True).strip()
        if not value.isdigit():
            break
        current = int(value)
    return False


class Lease:
    def __init__(self, root, environ=None):
        self.path = Path(root) / '.toolchain/locks/android-validation.lock'
        self.fd = None
        self.inherited = False
        self.environ = os.environ if environ is None else environ

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        supplied = [self.environ.get(PREFIX + key) for key in ('FD', 'OWNER', 'TOKEN')]
        if any(value is not None for value in supplied):
            if not all(supplied):
                raise Busy('Incomplete inherited validation lease')
            try:
                fd, owner, token = int(supplied[0]), int(supplied[1]), supplied[2]
                info, expected = os.fstat(fd), os.stat(self.path, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                    raise ValueError('Lease descriptor identity')
                record = json.loads(os.pread(fd, 2048, 0))
                if record != {'owner': owner, 'token': token} or not ancestor(owner):
                    raise ValueError('Lease owner/token mismatch')
                # A second open description must not be able to acquire the lock.
                probe = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        fcntl.flock(probe, fcntl.LOCK_UN)
                        raise ValueError('Inherited lease is not held')
                finally:
                    os.close(probe)
                self.fd, self.owner, self.token, self.inherited = os.dup(fd), owner, token, True
                return self
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise Busy('Invalid inherited validation lease') from error
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise Busy('Invalid validation lock file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner, self.token = os.getpid(), secrets.token_hex(24)
            data = json.dumps({'owner': self.owner, 'token': self.token}).encode()
            os.fchmod(fd, 0o600); os.ftruncate(fd, 0); os.pwrite(fd, data, 0); os.fsync(fd)
            self.fd = fd
            return self
        except BlockingIOError as error:
            os.close(fd)
            raise Busy('Android validation is busy') from error
        except BaseException:
            os.close(fd)
            raise

    def child_options(self, env=None):
        if self.fd is None:
            raise RuntimeError('Validation lease is closed')
        copied = dict(os.environ if env is None else env)
        copied.update({PREFIX+'FD': str(self.fd), PREFIX+'OWNER': str(self.owner), PREFIX+'TOKEN': self.token})
        return {'env': copied, 'pass_fds': (self.fd,)}

    def close(self):
        if self.fd is not None:
            # Closing an inherited descriptor must not unlock the parent's lease.
            if not self.inherited:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd); self.fd = None

    def __enter__(self): return self.acquire()
    def __exit__(self, *_): self.close()


def acquire_or_exit(root):
    try:
        lease = Lease(root).acquire()
    except Busy:
        print(json.dumps({'state': 'deferred-android-busy', 'published': False}))
        raise SystemExit(75)
    atexit.register(lease.close)
    return lease
