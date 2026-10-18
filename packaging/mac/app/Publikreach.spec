"""Standalone arm64 Cocoa bundle. Run through packaging/mac/build_app.sh."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

source = Path(SPECPATH)
root = source.parents[2]
datas = collect_data_files('vcoutreach') + collect_data_files('webview')
datas += collect_data_files('vcoutreach', includes=['web/scripts/*.py'], include_py_files=True)
datas += collect_data_files('googleapiclient')
for distribution in ('pywebview', 'google-auth', 'google-auth-oauthlib', 'google-api-python-client'):
    datas += copy_metadata(distribution)
hidden = collect_submodules('vcoutreach')
for module in ('google.auth', 'google.oauth2', 'google_auth_oauthlib', 'googleapiclient'):
    hidden += collect_submodules(module)
hidden += ['webview.platforms.cocoa', 'AppKit', 'Foundation', 'WebKit', 'PyObjCTools.AppHelper']

analysis = Analysis(
    [str(source / 'app_main.py')], pathex=[str(root), str(source)],
    binaries=[], datas=datas, hiddenimports=hidden,
    excludes=['webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.winforms',
              'webview.platforms.cef', 'webview.platforms.android', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name='Publikreach',
    debug=False, strip=False, upx=False, console=False, target_arch='arm64',
    codesign_identity=None,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='Publikreach')
app = BUNDLE(
    collection, name='Publikreach.app', icon=str(source / 'Publikreach.icns'),
    bundle_identifier='com.publik.reach',
    info_plist={
        'CFBundleName': 'Publikreach',
        'CFBundleDisplayName': 'Publikreach',
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleVersion': '1',
        'LSMinimumSystemVersion': '12.0',
        'LSMultipleInstancesProhibited': True,
        'NSHighResolutionCapable': True,
        'NSDocumentsFolderUsageDescription': 'Publikreach stores your outreach workspace in Documents.',
        'NSPrincipalClass': 'NSApplication',
    },
)
