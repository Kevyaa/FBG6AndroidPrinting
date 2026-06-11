# Ghost6 WiFi Print — сборка APK

Приложение написано на Python/Kivy, поэтому APK собирается **Buildozer'ом**,
а не Android Studio (она для Java/Kotlin и наш проект не съест).
Три способа, от простого к хардкорному.

---

## Способ 1 — GitHub Actions (рекомендую: без Линукса, без установки чего-либо)

1. Зарегистрируйся на github.com (если ещё нет).
2. Создай новый репозиторий (можно приватный): кнопка **New repository** → имя
   любое → Create.
3. Залей в него **всё содержимое этой папки** (main.py, ghost6_core.py,
   buildozer.spec и папку .github целиком). Проще всего: на странице репозитория
   **Add file → Upload files** → перетащи файлы. Папку .github/workflows/ с
   build-apk.yml создай через **Add file → Create new file**, в имени файла
   набери `.github/workflows/build-apk.yml` и вставь содержимое.
4. После пуша GitHub сам запустит сборку: вкладка **Actions** → задача
   «Build APK». Первая сборка идёт 20–40 минут (качает Android SDK/NDK),
   повторные — быстрее за счёт кэша.
5. Когда позеленеет — открой завершённую задачу, внизу раздел **Artifacts**,
   скачай `ghost6-wifi-print-apk`. Внутри zip — готовый APK.
6. Закинь APK на телефон, разреши установку из неизвестных источников, ставь.

Это debug-сборка: подписана отладочным ключом, ставится и работает как обычное
приложение. Для публикации в Google Play нужна release-подпись, но для личного
пользования debug — норм.

## Способ 2 — WSL (Windows Subsystem for Linux)

```bash
# в PowerShell от администратора (один раз):
wsl --install -d Ubuntu
# дальше внутри Ubuntu:
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf libtool \
    pkg-config zlib1g-dev libncurses5-dev libtinfo6 cmake libffi-dev libssl-dev
pip3 install --user buildozer cython==0.29.36
cd /mnt/c/путь/к/папке/ghost6_apk
~/.local/bin/buildozer android debug
# готовый APK появится в bin/
```

## Способ 3 — Docker

```bash
docker run --rm -v "%cd%":/home/user/hostcwd kivy/buildozer android debug
```

---

## Что внутри

- `main.py` — интерфейс (Kivy) + запрос разрешений Android
- `ghost6_core.py` — ядро: протокол стрима, флешка, мониторинг, Telegram, PNG
- `buildozer.spec` — конфиг сборки: разрешения (интернет, файлы), wakelock
  (телефон не уснёт во время печати), portrait, arm64+arm32
- `.github/workflows/build-apk.yml` — облачная сборка

## После установки

- G-code файлы клади в папку **Download** телефона.
- При первом запуске приложение спросит доступ к файлам — разреши.
- Телефон и принтер должны быть в одной WiFi-сети.
- На время стрима держи телефон на зарядке; wakelock уже включён в сборку,
  но оптимизацию батареи для приложения лучше отключить
  (Настройки → Приложения → Ghost6 WiFi Print → Батарея → Без ограничений).
