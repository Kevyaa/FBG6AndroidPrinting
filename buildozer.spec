[app]
# --- Ghost6 WiFi Print: управление FlyingBear Ghost 6 через MKS WiFi ---
title = Ghost6 WiFi Print
package.name = ghost6wifiprint
package.domain = org.kevya
source.dir = .
source.include_exts = py
version = 1.0

# только стандартная библиотека + kivy — никаких лишних рецептов
requirements = python3,kivy==2.3.0

orientation = portrait
fullscreen = 0

# не давать телефону спать во время печати
android.wakelock = True

# сеть (принтер + telegram) и файлы (выбор G-code из Download)
android.permissions = INTERNET, ACCESS_NETWORK_STATE, ACCESS_WIFI_STATE, WAKE_LOCK, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a

# в облачной сборке некому жать «y» — принимаем лицензии SDK автоматически
android.accept_sdk_license = True

# презентация
android.presplash_color = #1565c0

[buildozer]
log_level = 2
warn_on_root = 0
