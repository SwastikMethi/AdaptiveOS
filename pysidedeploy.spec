[app]
title = Adaptive OS Controller
project_dir = .
input_file = desktop_app.py
exec_directory = dist
project_file =
icon =

[python]
python_path = .venv/bin/python
packages = Nuitka==4.1.1
android_packages =

[qt]
qml_files =
excluded_qml_plugins =
modules = Core,Gui,Widgets
plugins = platforms,styles

[android]
wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]
macos.permissions =
mode = standalone
extra_args = --noinclude-qt-translations --macos-app-name='Adaptive OS Controller' --macos-signed-app-name=org.adaptiveos.controller --assume-yes-for-downloads --jobs=4

[buildozer]
mode = debug
recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =
