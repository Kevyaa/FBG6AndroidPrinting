# -*- coding: utf-8 -*-
"""
ghost6_core — ядро Ghost6 WiFi Print без GUI: протокол стрима с нумерацией строк,
переподключением и сменой пластика, загрузка на флешку, мониторинг M27,
анализ G-code, Telegram-уведомления, рендер PNG. Только стандартная библиотека.
Используется и десктопной (tkinter), и Android-версией (Kivy).
"""

import os
import re
import socket
import select
import queue
import threading
import time
import http.client
import json
import urllib.request
import urllib.parse
import configparser
import zlib
import struct
import bisect

CONTROL_PORT = 8080

UPLOAD_PORT = 80

SOCKET_TIMEOUT = 5

LONG_COMMANDS = ("M109", "M190", "M191", "G28", "G29", "G34", "M600")

OK_TIMEOUT = 20            # обычная строка: ждать ok до 20 с, дальше повтор по номеру строки

OK_TIMEOUT_MOVE = 6        # движения (G0/G1): потерянный ok переотправляем быстро,

OK_TIMEOUT_LONG = 1200     # нагрев/парковка: до 20 минут

RECONNECT_DELAY = 5        # пауза между попытками переподключения, с

RECONNECT_WINDOW = 600     # сколько всего ждать возвращения связи, с

SNAPSHOT_INTERVAL = 30 * 60  # фото прогресса в Telegram, с

TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
    'и':'i','й':'i','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'c','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def sanitize_filename(name: str) -> str:
    base, ext = os.path.splitext(name)
    out = []
    for ch in base:
        low = ch.lower()
        if low in TRANSLIT:
            t = TRANSLIT[low]
            out.append(t.upper() if ch.isupper() and t else t)
        elif ch.isalnum() or ch in '-_':
            out.append(ch)
        else:
            out.append('_')
    return (''.join(out)[:40] or 'print') + (ext.lower() if ext else '.gcode')

def clean_gcode_lines(path):
    """Прочитать файл, выкинуть комментарии и пустые строки."""
    lines = []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for raw in f:
            line = raw.split(';', 1)[0].strip()
            if line:
                lines.append(line)
    return lines

def fmt_dur(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"

def analyze_gcode(path):
    """Полный разбор файла: строки, слои, сегменты для визуализации,
    карта времени из комментариев Cura (;TIME / ;TIME_ELAPSED / ;LAYER)."""
    lines, line_layer, segments, time_map = [], [], [], []
    line_offsets = []          # байтовое смещение начала каждой строки в файле
    total_time = None
    layer, max_layer, saw_layer_comments = 0, 0, False
    x = y = z = e = 0.0
    abs_xy = abs_e = True
    byte_pos = 0
    with open(path, 'rb') as f:
        for raw_b in f:
            line_start = byte_pos
            byte_pos += len(raw_b)
            raw = raw_b.decode('utf-8', 'ignore')
            s = raw.strip()
            if s.startswith(';'):
                if s.startswith(';TIME:'):
                    try: total_time = float(s[6:])
                    except ValueError: pass
                elif s.startswith(';TIME_ELAPSED:'):
                    try: time_map.append((len(lines), float(s[14:])))
                    except ValueError: pass
                elif s.startswith(';LAYER:'):
                    try:
                        layer = max(0, int(s[7:])); saw_layer_comments = True
                        max_layer = max(max_layer, layer)
                    except ValueError: pass
                continue
            code = raw.split(';', 1)[0].strip()
            if not code:
                continue
            idx = len(lines)
            lines.append(code)
            line_offsets.append(line_start)
            u = code.upper()
            if u.startswith(('G0', 'G1')):
                nx, ny, nz, ne = x, y, z, e
                for tok in code.split()[1:]:
                    a = tok[0].upper()
                    try: v = float(tok[1:])
                    except ValueError: continue
                    if a == 'X': nx = v if abs_xy else x + v
                    elif a == 'Y': ny = v if abs_xy else y + v
                    elif a == 'Z': nz = v if abs_xy else z + v
                    elif a == 'E': ne = v if abs_e else e + v
                if not saw_layer_comments and nz > z + 0.01 and ne > e:
                    layer += 1; max_layer = max(max_layer, layer)
                if ne > e + 1e-6 and (abs(nx - x) > 1e-6 or abs(ny - y) > 1e-6):
                    segments.append((idx, layer, x, y, nx, ny))
                x, y, z, e = nx, ny, nz, ne
            elif u.startswith('G90'): abs_xy = abs_e = True
            elif u.startswith('G91'): abs_xy = abs_e = False
            elif u.startswith('M82'): abs_e = True
            elif u.startswith('M83'): abs_e = False
            elif u.startswith('G92'):
                for tok in code.split()[1:]:
                    a = tok[0].upper()
                    try: v = float(tok[1:])
                    except ValueError: continue
                    if a == 'E': e = v
                    elif a == 'X': x = v
                    elif a == 'Y': y = v
                    elif a == 'Z': z = v
            line_layer.append(layer)
    return {"lines": lines, "line_layer": line_layer, "segments": segments,
            "time_map": time_map, "total_time": total_time, "max_layer": max_layer,
            "line_offsets": line_offsets, "file_size": byte_pos}

def time_at_line(time_map, idx):
    """Оценка времени Cura для строки idx по карте ;TIME_ELAPSED (ступенчато)."""
    t = 0.0
    for i, sec in time_map:
        if i > idx:
            break
        t = sec
    return t

BED_W, BED_H = 255, 210   # стол Ghost 6, мм

class DirectStreamer(threading.Thread):
    """Построчная трансляция G-code в принтер по TCP с ожиданием 'ok'."""

    def __init__(self, ip, lines, on_progress, on_log, on_done):
        super().__init__(daemon=True)
        self.ip = ip
        self.lines = lines
        self.on_progress = on_progress   # (sent, total)
        self.on_log = on_log             # (text)
        self.on_done = on_done           # (ok: bool, message: str)
        self.pause_evt = threading.Event()   # установлен = пауза
        self.stop_evt = threading.Event()
        self.filchange_evt = threading.Event()        # запрошена смена пластика
        self.resume_change_evt = threading.Event()    # «продолжить печать» после смены
        self.inject_q = queue.Queue()                 # 'unload' / 'load'
        self.in_change = False
        self.abs_e = True
        self.t_print_start = None   # момент старта печати (после разогрева)
        self.hot_override = None    # температура сопла, заданная пользователем
        self.bed_override = None    # температура стола, заданная пользователем
        self._change_restore = None # (x,y,z,e) для возврата после смены пластика
        self.sock = None

    # --- управление снаружи ---
    def pause(self):
        self.pause_evt.set()
        self.on_log("Пауза: отправка остановлена (принтер доделает буфер и замрёт).")

    def resume(self):
        self.pause_evt.clear()
        self.on_log("Продолжаем.")

    def stop(self):
        self.stop_evt.set()
        self.pause_evt.clear()
        self.resume_change_evt.set()   # выдернуть из режима смены, если он активен

    # --- смена пластика ---
    def request_filament_change(self):
        self.filchange_evt.set()
        self.on_log("Смена пластика запрошена — остановлюсь на ближайшей строке.")

    def inject(self, what):            # 'unload' | 'load'
        self.inject_q.put(what)

    def finish_filament_change(self):
        self.resume_change_evt.set()

    # --- внутренности ---
    def _connect(self):
        self.sock = socket.create_connection((self.ip, CONTROL_PORT), timeout=SOCKET_TIMEOUT)
        self.sock.settimeout(10)  # только на отправку; чтение идёт через select
        self.rxbuf = b""

    def _read_line(self, timeout):
        """Прочитать одну строку от принтера. None = за timeout ничего не пришло.
        Через select + свой буфер — без makefile(), который ломается после таймаута."""
        deadline = time.time() + timeout
        while True:
            if b"\n" in self.rxbuf:
                raw, self.rxbuf = self.rxbuf.split(b"\n", 1)
                return raw.decode('ascii', 'ignore').strip()
            remain = deadline - time.time()
            if remain <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], min(remain, 1.0))
            if ready:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("Принтер закрыл соединение")
                self.rxbuf += data

    def _send_line(self, line, timeout):
        """Отправить строку и дождаться 'ok'. Возвращает True/False."""
        self.sock.sendall((line + "\r\n").encode('ascii', 'ignore'))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_evt.is_set():
                return False
            text = self._read_line(min(2.0, max(0.1, deadline - time.time())))
            if not text:
                continue  # тишина — ждём дальше (нагрев и т.п.)
            low = text.lower()
            if low.startswith('ok') or ' ok' in f' {low}':
                return True
            if 'error' in low:
                self.on_log(f"Принтер ругается: {text}")
                # Marlin после error обычно всё равно шлёт ok — ждём дальше
        return False

    S_RE = re.compile(r'S(\d+(?:\.\d+)?)')

    def _apply_override(self, line):
        """Подменить температуру в M104/M109/M140/M190, если задано переопределение.
        S0 (выключение нагрева в конце файла) не трогаем."""
        u = line.upper()
        ovr = None
        if self.bed_override and u.startswith(("M140", "M190")):
            ovr = self.bed_override
        elif self.hot_override and u.startswith(("M104", "M109")):
            ovr = self.hot_override
        if ovr is None:
            return line
        m = self.S_RE.search(line)
        if not m or float(m.group(1)) <= 0:
            return line
        return line[:m.start()] + f"S{ovr:.0f}" + line[m.end():]

    # ---- протокол с нумерацией и чексуммами (как у настоящих хостов) ----
    @staticmethod
    def _checksum(payload):
        cs = 0
        for ch in payload:
            cs ^= ord(ch)
        return cs

    def _numbered(self, n, line):
        body = f"N{n} {line}"
        return f"{body}*{self._checksum(body)}"

    RESEND_RE = re.compile(r'(?:resend|rs)\s*:?\s*(?:n\s*:?\s*)?(\d+)', re.I)

    def _transmit(self, payload):
        self.sock.sendall((payload + "\r\n").encode('ascii', 'ignore'))

    def _wait_ack(self, timeout):
        """Ждать подтверждение. Возвращает 'ok' | ('resend', n) | None (таймаут).
        Пока принтер шлёт хоть что-то (температуры, busy) — он жив и занят,
        повторы не запускаем, просто ждём (но не дольше OK_TIMEOUT_LONG)."""
        soft = time.time() + timeout
        hard = time.time() + max(timeout, OK_TIMEOUT_LONG)
        while time.time() < soft:
            if self.stop_evt.is_set():
                return None
            text = self._read_line(min(2.0, max(0.1, soft - time.time())))
            if not text:
                continue
            low = text.lower()
            m = self.RESEND_RE.search(low)
            if m:
                n = int(m.group(1))
                # после Resend прошивка досылает свой 'ok' — съедаем его,
                # чтобы он не зачёлся подтверждением следующей строки
                t_end = time.time() + 2.0
                while time.time() < t_end:
                    extra = self._read_line(0.5)
                    if extra and extra.lower().startswith('ok'):
                        break
                return ('resend', n)
            if low.startswith('ok'):
                return 'ok'
            if 'busy' in low or 'T:' in text or 'B:' in text:
                # принтер жив, просто занят (греется/паркуется) — продлеваем ожидание
                soft = min(time.time() + timeout, hard)
                continue
            if 'error' in low and 'checksum' not in low and 'line number' not in low:
                self.on_log(f"Принтер ругается: {text}")
        return None

    # ---- смена пластика без прерывания печати ----
    M114_RE = re.compile(r'X:\s*([-\d.]+)\s+Y:\s*([-\d.]+)\s+Z:\s*([-\d.]+)\s+E:\s*([-\d.]+)')

    def _do_plain(self, cmd, timeout=30):
        """Ненумерованная команда: отправить и дождаться ok (терпя отчёты температур)."""
        self._transmit(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self._read_line(1.0)
            if text and text.lower().startswith('ok'):
                return True
        return False

    def _query_m114(self):
        self._transmit("M114")
        deadline = time.time() + 5
        while time.time() < deadline:
            text = self._read_line(1.0)
            if not text:
                continue
            m = self.M114_RE.search(text)
            if m:
                # дочитать ok, чтобы не висел в буфере
                t = time.time() + 2
                while time.time() < t:
                    e = self._read_line(0.5)
                    if e and e.lower().startswith('ok'):
                        break
                return tuple(float(m.group(k)) for k in range(1, 5))
        return None

    def _filament_change(self):
        """Парковка → смена прутка кнопками → возврат в точку. False = печать остановили."""
        self.filchange_evt.clear()
        self.on_log("Смена пластика: жду окончания текущих движений…")
        self._do_plain("M400", 300)               # дождаться, пока буфер моторики доедет
        pos = None
        for _ in range(3):
            pos = self._query_m114()
            if pos:
                break
        if not pos:
            self.on_log("Не смог узнать позицию (M114) — смена отменена, печать продолжается.")
            return True
        x, y, z, e = pos
        self._change_restore = (x, y, z, e)
        self._do_plain("G91")
        self._do_plain("G1 E-2 F2400")            # ретракт, чтобы не сопливило
        self._do_plain("G1 Z10 F600")             # приподнять над деталью
        self._do_plain("G90")
        self._do_plain("G1 X10 Y10 F6000", 60)    # парковка в угол
        self.in_change = True
        self.on_log("Голова запаркована, СОПЛО ГОРЯЧЕЕ. Жми «Выгрузить», меняй пруток, "
                    "«Подать» до чистого цвета, затем «Продолжить печать».")
        while not self.resume_change_evt.is_set():
            if self.stop_evt.is_set():
                self.in_change = False
                return False
            try:
                cmd = self.inject_q.get(timeout=3)
            except queue.Empty:
                try:                               # keep-alive
                    self.sock.sendall(b"M105\r\n")
                    self._read_line(2.0)
                except OSError:
                    pass
                continue
            if cmd == 'unload':
                self.on_log("Выгружаю пруток…")
                self._do_plain("G91")
                self._do_plain("G1 E-10 F600", 60)
                self._do_plain("G1 E-70 F1200", 120)
                self._do_plain("G90")
                self.on_log("Готово — вытаскивай остаток и вставляй новый пруток до упора.")
            elif cmd == 'load':
                self.on_log("Подаю и продуваю…")
                self._do_plain("G91")
                self._do_plain("G1 E60 F300", 120)
                self._do_plain("G1 E25 F150", 120)
                self._do_plain("G90")
                self.on_log("Продувка готова. Цвет ещё грязный — жми «Подать» ещё раз.")
        self.resume_change_evt.clear()
        self.in_change = False
        if self.stop_evt.is_set():
            return False
        self.on_log("Возвращаюсь к печати…")
        self._restore_position()
        self.on_log("Продолжаем печать.")
        return True

    # ---- фаза разогрева перед стартом ----
    TEMP_HOT_RE = re.compile(r'^(?:M109|M104)\s.*?S(\d+(?:\.\d+)?)', re.I)
    TEMP_BED_RE = re.compile(r'^(?:M190|M140)\s.*?S(\d+(?:\.\d+)?)', re.I)
    M105_RE = re.compile(r'T0?:\s*([\d.]+)\s*/\s*([\d.]+).*?B:\s*([\d.]+)\s*/\s*([\d.]+)')

    def _extract_targets(self):
        """Вытащить целевые температуры из начала G-code."""
        hot = bed = None
        for line in self.lines[:300]:
            if hot is None:
                m = self.TEMP_HOT_RE.match(line)
                if m and float(m.group(1)) > 0:
                    hot = float(m.group(1))
            if bed is None:
                m = self.TEMP_BED_RE.match(line)
                if m and float(m.group(1)) > 0:
                    bed = float(m.group(1))
            if hot is not None and bed is not None:
                break
        return hot, bed

    def _drain(self, quiet=1.0):
        """Выбросить всё накопившееся в буфере (хвосты 'ok' от разогрева)."""
        while self._read_line(quiet) is not None:
            pass

    def _poll_temps(self):
        """M105 → (сопло_тек, сопло_цель, стол_тек, стол_цель) или None."""
        try:
            self.sock.sendall(b"M105\r\n")
        except OSError:
            return None
        t_end = time.time() + 3.0
        while time.time() < t_end:
            text = self._read_line(1.0)
            if not text:
                continue
            m = self.M105_RE.search(text)
            if m:
                return tuple(float(m.group(k)) for k in range(1, 5))
        return None

    def _preheat(self):
        """Нагреть сопло и стол до целей из файла ДО начала стрима.
        Возвращает False, если печать остановили во время разогрева."""
        hot, bed = self._extract_targets()
        if self.hot_override:
            hot = self.hot_override
        if self.bed_override:
            bed = self.bed_override
        if hot is None and bed is None:
            return True
        self.on_log("Разогрев перед стартом: "
                    + (f"сопло {hot:.0f}°C " if hot else "")
                    + (f"стол {bed:.0f}°C" if bed else ""))
        if bed:
            self._transmit(f"M140 S{bed:.0f}")
        if hot:
            self._transmit(f"M104 S{hot:.0f}")
        deadline = time.time() + 1500   # 25 минут на разогрев — с запасом
        last_report = 0.0
        while time.time() < deadline:
            if self.stop_evt.is_set():
                return False
            temps = self._poll_temps()
            if temps:
                th, _, tb, _ = temps
                hot_ok = hot is None or th >= hot - 2
                bed_ok = bed is None or tb >= bed - 2
                if time.time() - last_report > 10:
                    last_report = time.time()
                    self.on_log(f"Греемся: сопло {th:.0f}/{hot or 0:.0f}°C, стол {tb:.0f}/{bed or 0:.0f}°C")
                if hot_ok and bed_ok:
                    self.on_log("Температуры набраны, стартуем печать.")
                    self._drain(1.0)
                    return True
            time.sleep(3)
        self.on_log("Разогрев не уложился в 25 минут — что-то не так с нагревателями, стоп.")
        return False

    def run(self):
        total = len(self.lines)
        i = 0
        attempts = 0
        first_connect = True
        try:
            while True:   # внешний цикл: (пере)подключение
                try:
                    self._connect()
                    if first_connect:
                        self.on_log(f"Соединение установлено, строк к отправке: {total}")
                        self._transmit(self._numbered(0, "M110"))
                        self._wait_ack(5)  # ответ не критичен
                        # фаза разогрева: греем до целей из файла, потом стримим —
                        # M109/M190 внутри файла подтвердятся мгновенно
                        if not self._preheat():
                            self._abort_safely()
                            self.on_done(False, "Печать остановлена на этапе разогрева.")
                            return
                        # абсолютная (M82, у Cura по умолчанию) или относительная (M83) экструзия
                        self.abs_e = not any(l.upper().startswith('M83') for l in self.lines[:300])
                        self.t_print_start = time.time()
                        first_connect = False
                    else:
                        self._drain(1.0)
                        # синхронизировать счётчик строк: следующая ожидаемая = i+1
                        self._transmit(self._numbered(i, "M110"))
                        self._wait_ack(5)
                        # если обрыв случился посреди смены пластика — сначала вернуть голову
                        if self._change_restore:
                            self.on_log("Обрыв случился во время смены пластика — возвращаю голову на деталь.")
                            self._restore_position()
                        self.on_log(f"Связь восстановлена, продолжаю со строки {i + 1}.")
                        attempts = 0

                    while i < total:
                        if self.stop_evt.is_set():
                            self._abort_safely()
                            self.on_done(False, "Печать остановлена пользователем.")
                            return
                        while self.pause_evt.is_set():
                            if self.stop_evt.is_set():
                                self._abort_safely()
                                self.on_done(False, "Печать остановлена пользователем.")
                                return
                            # keep-alive, чтобы модуль не закрыл сокет от скуки
                            try:
                                self.sock.sendall(b"M105\r\n")
                                self._read_line(2.0)
                            except OSError:
                                pass
                            time.sleep(3)

                        if self.filchange_evt.is_set():
                            if not self._filament_change():
                                self._abort_safely()
                                self.on_done(False, "Печать остановлена пользователем.")
                                return

                        line = self._apply_override(self.lines[i])
                        cmd_word = line.split()[0].upper()
                        if cmd_word in LONG_COMMANDS:
                            timeout = OK_TIMEOUT_LONG
                        elif cmd_word in ("G0", "G1"):
                            timeout = OK_TIMEOUT_MOVE
                        else:
                            timeout = OK_TIMEOUT
                        self._transmit(self._numbered(i + 1, line))
                        ack = self._wait_ack(timeout)

                        if self.stop_evt.is_set():
                            self._abort_safely()
                            self.on_done(False, "Печать остановлена пользователем.")
                            return

                        if ack == 'ok':
                            i += 1
                            attempts = 0
                            if i % 20 == 0 or i == total:
                                self.on_progress(i, total)
                        elif isinstance(ack, tuple):
                            n = ack[1]
                            attempts += 1
                            self.on_log(f"Принтер просит повтор со строки {n} (попытка {attempts}/8).")
                            i = max(0, min(n - 1, total - 1))
                        else:  # таймаут — ok потерялся в WiFi-модуле, шлём строку повторно:
                            # прошивка по номеру строки сама поймёт, дубль это или потеря
                            attempts += 1
                            self.on_log(f"Потерян ответ на строку {i+1}, повторная отправка (попытка {attempts}/8).")

                        if attempts >= 8:
                            # глухой канал при живом сокете — лечим полным переподключением
                            attempts = 0
                            raise ConnectionError("канал не отвечает на 8 попыток подряд")
                    self.on_progress(total, total)
                    self.on_done(True, "Печать завершена. Все строки отправлены и подтверждены.")
                    return

                except OSError as e:
                    if self.stop_evt.is_set():
                        self.on_done(False, "Печать остановлена пользователем.")
                        return
                    self.in_change = False
                    self.resume_change_evt.clear()
                    try:
                        if self.sock:
                            self.sock.close()
                    except OSError:
                        pass
                    self.on_log(f"⚠ Обрыв связи на строке {i + 1}: {e}")
                    self.on_log(f"Пробую переподключиться (каждые {RECONNECT_DELAY} с, "
                                f"до {RECONNECT_WINDOW // 60} минут). Принтер ждёт на месте.")
                    if not self._reconnect_wait():
                        self.on_done(False, f"Связь не восстановилась за {RECONNECT_WINDOW // 60} минут. "
                                            f"Печать прервана на строке {i + 1} из {total}.")
                        return
        finally:
            try:
                if self.sock:
                    self.sock.close()
            except OSError:
                pass

    def _reconnect_wait(self):
        """Ждать возвращения принтера в сеть. True = можно подключаться заново."""
        deadline = time.time() + RECONNECT_WINDOW
        n = 0
        while time.time() < deadline:
            if self.stop_evt.is_set():
                return False
            try:
                probe = socket.create_connection((self.ip, CONTROL_PORT), timeout=3)
                probe.close()
                return True
            except OSError:
                n += 1
                if n % 6 == 0:
                    self.on_log(f"Всё ещё нет связи… ({int(deadline - time.time())} с до отбоя)")
                time.sleep(RECONNECT_DELAY)
        return False

    def _restore_position(self):
        """Вернуть голову в сохранённую точку после смены пластика / обрыва в смене."""
        x, y, z, e = self._change_restore
        self._do_plain("G90")
        self._do_plain(f"G1 X{x:.3f} Y{y:.3f} F6000", 60)
        self._do_plain(f"G1 Z{z:.3f} F600", 60)
        self._do_plain("G91")
        self._do_plain("G1 E2 F2400")             # вернуть ретракт
        self._do_plain("G90")
        if self.abs_e:
            self._do_plain(f"G92 E{e:.5f}")       # восстановить счётчик экструзии
        self._do_plain("M400", 60)
        self._drain(0.5)
        self._change_restore = None

    def _abort_safely(self):
        """Стоп: выключить нагрев, поднять сопло, отпустить моторы."""
        safety = ["M108", "M104 S0", "M140 S0", "M107",
                  "G91", "G1 Z10 F600", "G90", "M84"]
        try:
            for cmd in safety:
                self.sock.sendall((cmd + "\r\n").encode())
                time.sleep(0.3)
            self.on_log("Нагрев выключен, сопло поднято на 10 мм, моторы отпущены.")
        except OSError:
            self.on_log("Не удалось отправить команды безопасности — ВЫКЛЮЧИ НАГРЕВ С ЭКРАНА ПРИНТЕРА.")

def render_snapshot_png(segments, sent_idx, layer, w=620, h=540):
    """Нарисовать текущий слой в PNG без сторонних библиотек:
    пиксельный буфер + Брезенхэм + ручная сборка PNG (zlib из стандартной)."""
    buf = bytearray(b"\xff" * (w * h * 3))   # белый фон

    def put(px, py, r, g, b):
        if 0 <= px < w and 0 <= py < h:
            o = (py * w + px) * 3
            buf[o] = r; buf[o + 1] = g; buf[o + 2] = b

    def line(x0, y0, x1, y1, c, thick=1):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            for ox in range(-thick + 1, thick):
                for oy in range(-thick + 1, thick):
                    put(x0 + ox, y0 + oy, *c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy: err += dy; x0 += sx
            if e2 <= dx: err += dx; y0 += sy

    margin = 16
    s = min((w - 2 * margin) / BED_W, (h - 2 * margin) / BED_H)

    def pt(x, y):
        return int(margin + x * s), int(h - margin - y * s)

    bx0, by0 = pt(0, BED_H); bx1, by1 = pt(BED_W, 0)
    for (ax, ay, cx, cy) in ((bx0, by0, bx1, by0), (bx0, by1, bx1, by1),
                             (bx0, by0, bx0, by1), (bx1, by0, bx1, by1)):
        line(ax, ay, cx, cy, (136, 136, 136))

    GRAY, BLUE, RED = (214, 214, 214), (21, 101, 192), (229, 57, 53)
    head = None
    for idx, L, ax, ay, bx_, by_ in segments:       # сначала план — серым
        if L == layer and idx > sent_idx:
            line(*pt(ax, ay), *pt(bx_, by_), GRAY)
    for idx, L, ax, ay, bx_, by_ in segments:       # поверх — напечатанное
        if L == layer and idx <= sent_idx:
            line(*pt(ax, ay), *pt(bx_, by_), BLUE, 2)
            head = (bx_, by_)
    if head:
        hx, hy = pt(*head)
        for ox in range(-4, 5):
            for oy in range(-4, 5):
                if ox * ox + oy * oy <= 16:
                    put(hx + ox, hy + oy, *RED)

    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                # фильтр 0 для каждой строки
        raw += buf[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", comp)
            + chunk(b"IEND", b""))

CONFIG_PATH = os.path.join(os.path.expanduser("~"), "ghost6_wifi_print.ini")

def load_config():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    g = cp["main"] if "main" in cp else {}
    return {"ip": g.get("ip", "192.168.1."),
            "tg_token": g.get("tg_token", ""),
            "tg_chat": g.get("tg_chat", ""),
            "tg_on": g.get("tg_on", "0") == "1"}

def save_config(ip, tg_token, tg_chat, tg_on):
    cp = configparser.ConfigParser()
    cp["main"] = {"ip": ip, "tg_token": tg_token, "tg_chat": tg_chat,
                  "tg_on": "1" if tg_on else "0"}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            cp.write(f)
    except OSError:
        pass

class TelegramNotifier:
    """Уведомления в Telegram через Bot API. Все отправки — в фоне,
    падение телеграма никогда не мешает печати."""

    def __init__(self):
        self.token = ""
        self.chat_id = ""
        self.enabled = False
        self.api_base = "https://api.telegram.org"   # подменяется в тестах
        self._warned = False

    def _api(self, method, params, timeout=10):
        url = f"{self.api_base}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))

    def send(self, text, log=None):
        """Отправить сообщение в фоне. Ошибки глотаем (один раз пишем в журнал)."""
        if not (self.enabled and self.token and self.chat_id):
            return
        def work():
            try:
                self._api("sendMessage", {"chat_id": self.chat_id, "text": text})
            except Exception as e:
                if not self._warned and log:
                    self._warned = True
                    log(f"Telegram недоступен ({e}) — печать продолжается, уведомления молчат.")
        threading.Thread(target=work, daemon=True).start()

    def send_photo(self, png_bytes, caption, log=None):
        """Отправить фото в фоне (multipart вручную, без сторонних библиотек)."""
        if not (self.enabled and self.token and self.chat_id):
            return
        def work():
            try:
                boundary = "----g6wp" + hex(int(time.time() * 1000))[2:]
                parts = []
                def field(name, val):
                    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                                  f'name="{name}"\r\n\r\n{val}\r\n').encode("utf-8"))
                field("chat_id", self.chat_id)
                if caption:
                    field("caption", caption)
                parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                              f'name="photo"; filename="progress.png"\r\n'
                              f"Content-Type: image/png\r\n\r\n").encode())
                parts.append(png_bytes)
                parts.append(b"\r\n")
                parts.append(f"--{boundary}--\r\n".encode())
                body = b"".join(parts)
                req = urllib.request.Request(
                    f"{self.api_base}/bot{self.token}/sendPhoto", data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                urllib.request.urlopen(req, timeout=30).read()
            except Exception as e:
                if not self._warned and log:
                    self._warned = True
                    log(f"Telegram недоступен ({e}) — печать продолжается, уведомления молчат.")
        threading.Thread(target=work, daemon=True).start()

    def test(self):
        """Синхронная проверка. Возвращает (ok, сообщение)."""
        try:
            r = self._api("sendMessage", {"chat_id": self.chat_id,
                                          "text": "✅ Ghost6 WiFi Print: связь с ботом работает."})
            return (True, "Сообщение отправлено, проверь Telegram.") if r.get("ok")                 else (False, f"Telegram ответил отказом: {r}")
        except Exception as e:
            return False, f"Не получилось: {e}"

    def find_chat_id(self):
        """Вытащить chat_id из последних сообщений боту (getUpdates)."""
        try:
            r = self._api("getUpdates", {"limit": 10})
            if not r.get("ok"):
                return None, f"Telegram ответил отказом: {r}"
            for upd in reversed(r.get("result", [])):
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat", {})
                if chat.get("id"):
                    who = chat.get("first_name") or chat.get("title") or ""
                    return str(chat["id"]), who
            return None, "Бот не видит сообщений. Напиши боту что-нибудь (хоть «привет») и нажми ещё раз."
        except Exception as e:
            return None, f"Не получилось: {e}"

def parse_m27(raw):
    """Достать позицию из ответа M27: (байт, всего) либо (процент, 100)."""
    m = re.search(r'byte\s+(\d+)\s*/\s*(\d+)', raw, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'M27\s+(\d+)', raw)
    if m:
        return int(m.group(1)), 100
    return None

class SDMonitor(threading.Thread):
    """Наблюдение за печатью, идущей с флешки: опрашивает M27/M997/M105
    и превращает байтовую позицию файла в строку/слой для визуализации."""

    def __init__(self, link, meta, on_update, on_log, on_done):
        super().__init__(daemon=True)
        self.link = link
        self.meta = meta
        self.on_update = on_update   # (idx, layer, pct, temps, state)
        self.on_log = on_log
        self.on_done = on_done       # (reason)
        self.stop_evt = threading.Event()

    def stop(self):
        self.stop_evt.set()

    def run(self):
        offsets = self.meta["line_offsets"]
        fsize = max(1, self.meta["file_size"])
        idle_streak = 0
        seen_printing = False
        while not self.stop_evt.is_set():
            state = self.link.status_state()
            if state == "PRINTING":
                seen_printing = True
                idle_streak = 0
            elif state in ("IDLE", None):
                idle_streak += 1
                if seen_printing and idle_streak >= 3:
                    self.on_done("Печать с флешки завершена (принтер перешёл в IDLE).")
                    return
                if not seen_printing and idle_streak >= 6:
                    self.on_done("Принтер ничего не печатает — мониторить нечего.")
                    return
            try:
                raw = self.link.send_gcode("M27", wait=1.5)
            except OSError:
                raw = ""
            pos = parse_m27(raw)
            if pos:
                cur, total = pos
                byte_pos = cur if total != 100 else int(fsize * cur / 100)
                pct = 100.0 * byte_pos / fsize
                idx = max(0, bisect.bisect_right(offsets, byte_pos) - 1)
                layer = self.meta["line_layer"][idx] if idx < len(self.meta["line_layer"]) else 0
                temps = self.link.get_temps()
                self.on_update(idx, layer, pct, temps, state or "?")
            for _ in range(5):
                if self.stop_evt.is_set():
                    return
                time.sleep(1)

class PrinterLink:
    """Короткие команды и HTTP-загрузка (режим 2)."""

    def __init__(self):
        self.ip = None

    def send_gcode(self, command, wait=1.0):
        if not self.ip:
            raise ConnectionError("IP не задан")
        with socket.create_connection((self.ip, CONTROL_PORT), timeout=SOCKET_TIMEOUT) as s:
            s.sendall((command.strip() + "\r\n").encode('ascii', 'ignore'))
            s.settimeout(wait)
            chunks = []
            t0 = time.time()
            while time.time() - t0 < wait:
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                except socket.timeout:
                    break
            return b''.join(chunks).decode('ascii', 'ignore')

    def ping(self):
        try:
            self.send_gcode("M105", wait=1.5)
            return True
        except OSError:
            return False

    def get_temps(self):
        try:
            raw = self.send_gcode("M105", wait=1.0)
            m = re.search(r"T0?:\s*([\d.]+)\s*/\s*([\d.]+).*?B:\s*([\d.]+)\s*/\s*([\d.]+)", raw)
            if m:
                return f"Сопло {m.group(1)}/{m.group(2)}°C  Стол {m.group(3)}/{m.group(4)}°C"
        except OSError:
            pass
        return ""

    def upload(self, filepath, remote_name, progress_cb=None):
        """Залить файл. Возвращает 'ok' | 'unknown' | 'fail'.
        'unknown' = все байты ушли, но модуль бросил соединение, не ответив —
        у MKS это штатное хамство, факт загрузки надо проверять списком файлов."""
        size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            data = f.read()
        conn = http.client.HTTPConnection(self.ip, UPLOAD_PORT, timeout=600)
        sent_all = False
        try:
            conn.putrequest("POST", f"/upload?X-Filename={remote_name}")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            chunk = 4096
            for i in range(0, size, chunk):
                conn.send(data[i:i + chunk])
                if progress_cb:
                    progress_cb(min(i + chunk, size), size)
            sent_all = True
            resp = conn.getresponse()
            body = resp.read().decode('ascii', 'ignore')
            if resp.status != 200:
                return 'fail'
            low = body.lower()
            m = re.search(r'"?err"?\s*[:=]\s*(\d+)', low)
            if m:                                  # {"err":0} = успех, а не ошибка
                return 'ok' if m.group(1) == '0' else 'fail'
            return 'fail' if 'error' in low else 'ok'
        except (http.client.HTTPException, OSError):
            return 'unknown' if sent_all else 'fail'
        finally:
            conn.close()

    def file_on_printer(self, remote_name):
        """Проверить по M20, лёг ли файл на флешку. None = список не получили."""
        try:
            raw = self.send_gcode("M20", wait=3.0)
        except OSError:
            return None
        target = remote_name.lower()
        short = target[:8]
        seen_any = False
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.lower().startswith(('begin', 'end', 'ok')):
                continue
            seen_any = True
            nm = line.split()[0].lower()
            if nm == target or nm.startswith(short):
                return True
        return False if seen_any else None

    def list_files(self):
        """Список файлов на флешке: [(имя, размер_байт|None), ...]. None = не получили."""
        try:
            raw = self.send_gcode("M20", wait=3.0)
        except OSError:
            return None
        files = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.lower().startswith(('begin', 'end', 'ok', 'echo')):
                continue
            parts = line.split()
            name = parts[0]
            if '.' not in name:
                continue
            size = None
            if len(parts) > 1 and parts[1].isdigit():
                size = int(parts[1])
            files.append((name, size))
        return files

    def delete_file(self, name):
        try:
            self.send_gcode(f"M30 {name}", wait=2.0)
            return True
        except OSError:
            return False

    def status_state(self):
        try:
            raw = self.send_gcode("M997", wait=1.5)
            m = re.search(r"M997\s+(\w+)", raw)
            return m.group(1) if m else None
        except OSError:
            return None

    def start_print(self, filename):
        self.send_gcode(f"M23 {filename}")
        self.send_gcode("M24")

    def emergency_stop(self):
        """Остановить печать в любом режиме: стоп SD-печати, нагрев в ноль,
        поднять сопло, отпустить моторы."""
        for cmd in ("M108", "M25", "M26", "M104 S0", "M140 S0", "M107",
                    "G91", "G1 Z10 F600", "G90", "M84"):
            try:
                self.send_gcode(cmd, wait=0.6)
            except OSError:
                pass
