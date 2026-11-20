# PyInstaller spec: GoldenRigServer.exe + rig_certtool.exe sharing one runtime folder.
# Build with installer\build.ps1 (or: pyinstaller --noconfirm installer\golden_rig.spec)
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
EXCLUDES = ['pytest', '_pytest', 'tkinter', 'unittest.mock', 'pydoc_data']

server = Analysis(
    [os.path.join(ROOT, 'rig_launcher.py')],
    pathex=[ROOT],
    hiddenimports=['serial_at_backend', 'serial.tools.list_ports', 'zeroconf', 'ifaddr'],
    excludes=EXCLUDES,
)
certtool = Analysis(
    [os.path.join(ROOT, 'rig_certtool.py')],
    pathex=[ROOT],
    excludes=EXCLUDES + ['bleak', 'serial', 'zeroconf'],
)

server_exe = EXE(PYZ(server.pure), server.scripts, [], exclude_binaries=True,
                 name='GoldenRigServer', console=True, upx=False)
certtool_exe = EXE(PYZ(certtool.pure), certtool.scripts, [], exclude_binaries=True,
                   name='rig_certtool', console=True, upx=False)

COLLECT(server_exe, server.binaries, server.datas,
        certtool_exe, certtool.binaries, certtool.datas,
        name='GoldenRig', upx=False)
