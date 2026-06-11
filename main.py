# -*- coding: utf-8 -*-
"""
Ghost6 WiFi Print — Android-версия (Kivy, запускается в Pydroid 3).
Положи рядом ghost6_core.py. Всё умеет то же, что и десктопная:
стрим с компа/телефона, загрузка на флешку, мониторинг идущей печати,
смена пластика, переопределение температур, Telegram, визуализация слоя.
"""

import os
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.widget import Widget
from kivy.graphics import Color, Line, Rectangle, Ellipse

import ghost6_core as core

# --- специфика Android (в APK модуль android есть, на десктопе — нет) ---
ON_ANDROID = False
try:
    from android.permissions import request_permissions, Permission  # type: ignore
    ON_ANDROID = True
except ImportError:
    pass

GCODE_DIR = "/storage/emulated/0/Download" if os.path.isdir("/storage/emulated/0") \
    else os.path.expanduser("~")


def ui(fn):
    """Выполнить fn в главном потоке Kivy."""
    Clock.schedule_once(lambda dt: fn(), 0)


class VizWidget(Widget):
    """Послойная визуализация: серое — план слоя, синее — напечатано, красное — голова."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.segments = []
        self.layer_start = {}
        self.cur_layer = None
        self.done_upto = -1
        self.head = None
        self.bind(size=lambda *a: self.redraw(), pos=lambda *a: self.redraw())

    def _scale(self):
        m = 10
        s = min((self.width - 2 * m) / core.BED_W, (self.height - 2 * m) / core.BED_H)
        return max(s, 0.01), m

    def _pt(self, x, y):
        s, m = self._scale()
        return self.x + m + x * s, self.y + m + y * s

    def set_data(self, segments):
        self.segments = segments
        self.layer_start = {}
        for i, seg in enumerate(segments):
            self.layer_start.setdefault(seg[1], i)
        self.cur_layer = None
        self.done_upto = -1
        self.head = None
        self.redraw()

    def progress(self, sent_idx, layer):
        if layer != self.cur_layer:
            self.cur_layer = layer
        self.done_upto = sent_idx
        # запомнить позицию головы
        i = self.layer_start.get(layer)
        if i is not None:
            head = None
            j = i
            while j < len(self.segments) and self.segments[j][1] == layer:
                if self.segments[j][0] <= sent_idx:
                    head = (self.segments[j][4], self.segments[j][5])
                j += 1
            self.head = head
        self.redraw()

    def redraw(self):
        self.canvas.clear()
        with self.canvas:
            Color(1, 1, 1, 1)
            Rectangle(pos=self.pos, size=self.size)
            Color(0.53, 0.53, 0.53, 1)
            x0, y0 = self._pt(0, 0)
            x1, y1 = self._pt(core.BED_W, core.BED_H)
            Line(rectangle=(x0, y0, x1 - x0, y1 - y0))
            L = self.cur_layer
            if L is None:
                return
            i = self.layer_start.get(L)
            if i is None:
                return
            gray_pts, blue_pts = [], []
            j = i
            while j < len(self.segments) and self.segments[j][1] == L:
                idx, _, ax, ay, bx, by = self.segments[j]
                pts = (*self._pt(ax, ay), *self._pt(bx, by))
                (blue_pts if idx <= self.done_upto else gray_pts).append(pts)
                j += 1
            Color(0.84, 0.84, 0.84, 1)
            for p in gray_pts:
                Line(points=p, width=1)
            Color(0.08, 0.40, 0.75, 1)
            for p in blue_pts:
                Line(points=p, width=1.4)
            if self.head:
                Color(0.9, 0.22, 0.21, 1)
                hx, hy = self._pt(*self.head)
                Ellipse(pos=(hx - 5, hy - 5), size=(10, 10))


class Ghost6App(App):
    title = "Ghost6 WiFi Print"

    # ---------- построение интерфейса ----------
    def build(self):
        self.link = core.PrinterLink()
        self.tg = core.TelegramNotifier()
        self.cfg = core.load_config()
        self.streamer = None
        self.sd_monitor = None
        self.selected_file = None
        self.meta = None
        self._milestones = set()
        self._last_snap = 0.0
        self._sd_names = []

        root = ScrollView()
        col = GridLayout(cols=1, spacing=6, padding=8, size_hint_y=None)
        col.bind(minimum_height=col.setter("height"))
        root.add_widget(col)

        def row(h=46):
            r = BoxLayout(size_hint_y=None, height=h, spacing=4)
            col.add_widget(r)
            return r

        # подключение
        r = row()
        self.ip_in = TextInput(text=self.cfg["ip"], multiline=False, hint_text="IP принтера")
        r.add_widget(self.ip_in)
        r.add_widget(Button(text="Подключиться", size_hint_x=0.6, on_release=lambda *a: self.on_connect()))
        self.conn_lbl = Label(text="не подключено", size_hint_y=None, height=24)
        col.add_widget(self.conn_lbl)

        # файл
        r = row()
        self.file_lbl = Label(text="файл не выбран", shorten=True)
        r.add_widget(self.file_lbl)
        r.add_widget(Button(text="Выбрать G-code", size_hint_x=0.6, on_release=lambda *a: self.on_pick()))

        # визуализация
        self.viz = VizWidget(size_hint_y=None, height=300)
        col.add_widget(self.viz)
        self.viz_lbl = Label(text="—", size_hint_y=None, height=24)
        col.add_widget(self.viz_lbl)
        self.mon_btn = Button(text="👁 Мониторить печать с флешки", size_hint_y=None, height=46,
                              on_release=lambda *a: self.on_monitor())
        col.add_widget(self.mon_btn)

        # стрим
        self.stream_btn = Button(text="▶ Печатать напрямую (стрим)", size_hint_y=None, height=52,
                                 on_release=lambda *a: self.on_stream())
        col.add_widget(self.stream_btn)
        r = row()
        self.pause_btn = Button(text="⏸ Пауза", on_release=lambda *a: self.on_pause())
        self.resume_btn = Button(text="▶ Дальше", on_release=lambda *a: self.on_resume())
        self.stop_btn = Button(text="⏹ Стоп", on_release=lambda *a: self.on_stop())
        for b in (self.pause_btn, self.resume_btn, self.stop_btn):
            r.add_widget(b)
        r = row()
        r.add_widget(Button(text="🔄 Смена пластика", on_release=lambda *a: self.on_filchange()))
        r.add_widget(Button(text="⏏ Выгрузить", on_release=lambda *a: self.on_inject('unload')))
        r.add_widget(Button(text="⤵ Подать", on_release=lambda *a: self.on_inject('load')))
        r.add_widget(Button(text="▶ Продолжить", on_release=lambda *a: self.on_finish_change()))
        r = row(40)
        r.add_widget(Label(text="Сопло °C:", size_hint_x=0.5))
        self.hot_in = TextInput(multiline=False, input_filter="float", size_hint_x=0.4)
        r.add_widget(self.hot_in)
        r.add_widget(Label(text="Стол °C:", size_hint_x=0.5))
        self.bed_in = TextInput(multiline=False, input_filter="float", size_hint_x=0.4)
        r.add_widget(self.bed_in)
        r.add_widget(Label(text="(пусто = из файла)", size_hint_x=0.8))
        self.stream_lbl = Label(text="—", size_hint_y=None, height=24)
        col.add_widget(self.stream_lbl)

        # флешка
        self.upload_btn = Button(text="Загрузить на флешку и напечатать", size_hint_y=None, height=46,
                                 on_release=lambda *a: self.on_upload())
        col.add_widget(self.upload_btn)
        r = row()
        self.sd_spin = Spinner(text="файлы на флешке…", values=[])
        r.add_widget(self.sd_spin)
        r.add_widget(Button(text="⟳", size_hint_x=0.2, on_release=lambda *a: self.on_sd_refresh()))
        r.add_widget(Button(text="▶", size_hint_x=0.2, on_release=lambda *a: self.on_sd_print()))
        r.add_widget(Button(text="🗑", size_hint_x=0.2, on_release=lambda *a: self.on_sd_delete()))

        # аварийный стоп
        est = Button(text="⏹ ОСТАНОВИТЬ ПЕЧАТЬ (аварийно)", size_hint_y=None, height=56,
                     background_color=(0.78, 0.16, 0.16, 1), on_release=lambda *a: self.on_emergency())
        col.add_widget(est)

        # telegram
        r = row(40)
        self.tg_token_in = TextInput(text=self.cfg["tg_token"], multiline=False,
                                     password=True, hint_text="Токен бота")
        r.add_widget(self.tg_token_in)
        r = row(40)
        self.tg_chat_in = TextInput(text=self.cfg["tg_chat"], multiline=False, hint_text="Chat ID")
        r.add_widget(self.tg_chat_in)
        r.add_widget(Button(text="Найти ID", size_hint_x=0.5, on_release=lambda *a: self.on_tg_find()))
        r.add_widget(Button(text="Тест", size_hint_x=0.4, on_release=lambda *a: self.on_tg_test()))
        self.tg_btn = Button(text=("Уведомления: ВКЛ" if self.cfg["tg_on"] else "Уведомления: выкл"),
                             size_hint_y=None, height=40, on_release=lambda *a: self.on_tg_toggle())
        self.tg_enabled = self.cfg["tg_on"]
        col.add_widget(self.tg_btn)

        # журнал
        self.log_lbl = Label(text="", size_hint_y=None, halign="left", valign="top",
                             text_size=(Window.width - 24, None))
        self.log_lbl.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        col.add_widget(self.log_lbl)
        self._loglines = []

        self._tg_apply()
        if ON_ANDROID:
            # доступ к файлам нужен для выбора G-code из Download
            request_permissions([Permission.READ_EXTERNAL_STORAGE,
                                 Permission.WRITE_EXTERNAL_STORAGE])
        return root

    # ---------- журнал и уведомления ----------
    TG_EVENTS = (("Обрыв связи", "⚠️"), ("Связь восстановлена", "🔌"),
                 ("запаркована", "🟡"), ("Связь не восстановилась", "🛑"),
                 ("Аварийный стоп", "🛑"))

    def log(self, text):
        for marker, emoji in self.TG_EVENTS:
            if marker in text:
                self.tg.send(f"{emoji} {text}", log=self._log_plain)
                break
        self._log_plain(text)

    def _log_plain(self, text):
        def do():
            self._loglines.append(time.strftime("[%H:%M:%S] ") + text)
            self._loglines = self._loglines[-60:]
            self.log_lbl.text = "\n".join(self._loglines)
            self.log_lbl.text_size = (Window.width - 24, None)
        ui(do)

    def _tg_apply(self):
        self.tg.token = self.tg_token_in.text.strip()
        self.tg.chat_id = self.tg_chat_in.text.strip()
        self.tg.enabled = self.tg_enabled
        core.save_config(self.ip_in.text.strip(), self.tg.token, self.tg.chat_id, self.tg.enabled)

    # ---------- подключение и файл ----------
    def on_connect(self):
        self.link.ip = self.ip_in.text.strip()
        self.conn_lbl.text = "проверяю…"
        def work():
            ok = self.link.ping()
            def done():
                self.conn_lbl.text = "✓ на связи" if ok else "✗ нет ответа"
                if ok:
                    self._tg_apply()
                    self.log("Принтер отвечает. " + (self.link.get_temps() or ""))
                    self.on_sd_refresh()
                else:
                    self.log("Принтер не отвечает: проверь IP и сеть (телефон и принтер — в одном WiFi).")
            ui(done)
        threading.Thread(target=work, daemon=True).start()

    def on_pick(self):
        chooser = FileChooserListView(path=GCODE_DIR, filters=["*.gcode", "*.gco", "*.g"])
        box = BoxLayout(orientation="vertical")
        box.add_widget(chooser)
        btn = Button(text="Выбрать", size_hint_y=None, height=48)
        box.add_widget(btn)
        pop = Popup(title="Выбери G-code", content=box, size_hint=(0.95, 0.95))
        def choose(*a):
            if chooser.selection:
                self.selected_file = chooser.selection[0]
                self.file_lbl.text = os.path.basename(self.selected_file)
                pop.dismiss()
        btn.bind(on_release=choose)
        pop.open()

    # ---------- стрим ----------
    def on_stream(self):
        if not self.selected_file:
            self.log("Сначала выбери G-code файл.")
            return
        if self.streamer and self.streamer.is_alive():
            self.log("Стрим уже идёт.")
            return
        self.log("Анализирую файл… Телефон не блокируй и держи на зарядке всю печать!")
        def work():
            meta = core.analyze_gcode(self.selected_file)
            ui(lambda: self._start_stream(meta))
        threading.Thread(target=work, daemon=True).start()

    def _start_stream(self, meta):
        self.meta = meta
        lines = meta["lines"]
        n_layers = meta["max_layer"] + 1
        est = meta["total_time"]
        self.log(f"Файл: {len(lines)} строк, {n_layers} слоёв"
                 + (f", оценка {core.fmt_dur(est)}." if est else "."))
        self.viz.set_data(meta["segments"])
        self._milestones = set()
        self._last_snap = time.time()
        self.tg.send(f"🖨 Печать начата: {os.path.basename(self.selected_file)}"
                     + (f", оценка {core.fmt_dur(est)}" if est else ""), log=self._log_plain)

        def on_progress(sent, total):
            idx = sent - 1
            layer = meta["line_layer"][idx] if idx < len(meta["line_layer"]) else 0
            pct = 100 * sent / total
            t0 = self.streamer.t_print_start if self.streamer else None
            elapsed = (time.time() - t0) if t0 else 0
            remaining = None
            if est and meta["time_map"]:
                est_el = core.time_at_line(meta["time_map"], idx)
                if est_el > 60 and elapsed > 60:
                    remaining = max(0, (est - est_el) * (elapsed / est_el))
                else:
                    remaining = max(0, est - est_el)
            txt = (f"{pct:.1f}% · слой {layer + 1}/{n_layers}"
                   + (f" · осталось ~{core.fmt_dur(remaining)}" if remaining is not None else ""))
            ui(lambda: (self.viz.progress(idx, layer),
                        setattr(self.stream_lbl, "text", txt),
                        setattr(self.viz_lbl, "text", f"Слой {layer + 1}/{n_layers}")))
            for mark in (25, 50, 75):
                if pct >= mark and mark not in self._milestones:
                    self._milestones.add(mark)
                    self.tg.send(f"📈 {mark}% · слой {layer + 1}/{n_layers}", log=self._log_plain)
            if self.tg.enabled and time.time() - self._last_snap >= core.SNAPSHOT_INTERVAL:
                self._last_snap = time.time()
                segs = meta["segments"]
                def snap(idx=idx, layer=layer, txt=txt):
                    png = core.render_snapshot_png(segs, idx, layer)
                    self.tg.send_photo(png, "🖼 " + txt, log=self._log_plain)
                threading.Thread(target=snap, daemon=True).start()

        def on_done(ok, msg):
            self.log(msg)
            t0 = self.streamer.t_print_start if self.streamer else None
            took = f" за {core.fmt_dur(time.time() - t0)}" if (ok and t0) else ""
            self.tg.send((f"✅ Печать завершена{took}" if ok else f"❌ {msg}"), log=self._log_plain)

        self.streamer = core.DirectStreamer(self.link.ip, lines, on_progress, self.log, on_done)
        for widget, attr in ((self.hot_in, "hot_override"), (self.bed_in, "bed_override")):
            raw = widget.text.strip()
            if raw:
                try:
                    v = float(raw)
                    if 0 < v <= 300:
                        setattr(self.streamer, attr, v)
                        self.log(f"Температура переопределена: {attr.split('_')[0]} {v:.0f}°C.")
                except ValueError:
                    pass
        self.streamer.start()

    def on_pause(self):
        if self.streamer and self.streamer.is_alive():
            self.streamer.pause()

    def on_resume(self):
        if self.streamer and self.streamer.is_alive():
            self.streamer.resume()

    def on_stop(self):
        if self.streamer and self.streamer.is_alive():
            self.streamer.stop()

    def on_filchange(self):
        if self.streamer and self.streamer.is_alive():
            self.streamer.request_filament_change()

    def on_inject(self, what):
        if self.streamer and self.streamer.in_change:
            self.streamer.inject(what)
        else:
            self.log("Сначала дождись парковки головы (смотри журнал).")

    def on_finish_change(self):
        if self.streamer and self.streamer.in_change:
            self.streamer.finish_filament_change()

    def on_emergency(self):
        def work():
            if self.streamer and self.streamer.is_alive():
                self.streamer.stop()
            else:
                self.link.emergency_stop()
                self.log("Аварийный стоп: печать остановлена, нагрев выключен, сопло поднято.")
        threading.Thread(target=work, daemon=True).start()

    # ---------- флешка ----------
    def on_upload(self):
        if not self.selected_file:
            self.log("Сначала выбери G-code файл.")
            return
        remote = core.sanitize_filename(os.path.basename(self.selected_file))
        def work():
            try:
                res = self.link.upload(self.selected_file, remote)
                if res == 'fail':
                    self.log("Принтер ответил отказом — проверь флешку.")
                    return
                if res == 'unknown':
                    self.log("Модуль бросил соединение (его манера) — проверяю по факту…")
                time.sleep(2)
                present = self.link.file_on_printer(remote)
                if present is False:
                    self.log(f"Файла {remote} на флешке не видно — повтори загрузку.")
                    return
                self.link.start_print(remote)
                time.sleep(3)
                state = self.link.status_state()
                if state == "PRINTING":
                    self.log("Печать запущена — статус PRINTING подтверждён. ✅")
                    self.tg.send(f"🖨 Печать с флешки: {remote}", log=self._log_plain)
                    ui(self.on_monitor)
                else:
                    self.log(f"Старт ушёл, статус: {state or 'неизвестен'} — глянь на экран принтера.")
                ui(self.on_sd_refresh)
            except OSError as e:
                self.log(f"Ошибка связи при загрузке: {e}")
        threading.Thread(target=work, daemon=True).start()
        self.log(f"Загружаю {remote}… (WiFi у MKS медленный, жди)")

    def on_sd_refresh(self):
        def work():
            files = self.link.list_files()
            def done():
                if files is None:
                    self.log("Список файлов не получил.")
                    return
                self._sd_names = [n for n, _ in files]
                self.sd_spin.values = [n + (f"  ({s/1024/1024:.1f} МБ)" if s else "") for n, s in files]
                self.sd_spin.text = self.sd_spin.values[0] if files else "флешка пустая"
            ui(done)
        threading.Thread(target=work, daemon=True).start()

    def _sd_selected(self):
        if not self._sd_names:
            return None
        sel = self.sd_spin.text.split("  (")[0]
        return sel if sel in self._sd_names else None

    def on_sd_print(self):
        name = self._sd_selected()
        if not name:
            self.log("Выбери файл из списка (⟳, потом тапни по списку).")
            return
        if self.streamer and self.streamer.is_alive():
            self.log("Идёт стрим — вторую печать не запускаю.")
            return
        def work():
            self.link.start_print(name)
            time.sleep(3)
            state = self.link.status_state()
            if state == "PRINTING":
                self.log(f"Печать {name} запущена. ✅")
                self.tg.send(f"🖨 Печать с флешки: {name}", log=self._log_plain)
            else:
                self.log(f"Старт ушёл, статус: {state or 'неизвестен'}.")
        threading.Thread(target=work, daemon=True).start()

    def on_sd_delete(self):
        name = self._sd_selected()
        if not name:
            self.log("Выбери файл из списка.")
            return
        def work():
            ok = self.link.delete_file(name)
            self.log(f"Файл {name} удалён." if ok else f"Не удалил {name}.")
            ui(self.on_sd_refresh)
        threading.Thread(target=work, daemon=True).start()

    # ---------- мониторинг ----------
    def on_monitor(self):
        if self.sd_monitor and self.sd_monitor.is_alive():
            self.sd_monitor.stop()
            self.mon_btn.text = "👁 Мониторить печать с флешки"
            self.log("Мониторинг остановлен.")
            return
        if self.streamer and self.streamer.is_alive():
            self.log("Идёт стрим — у него своя визуализация.")
            return
        if not self.selected_file:
            self.log("Выбери тот же G-code, который сейчас печатается.")
            return
        self.log("Анализирую файл для мониторинга…")
        def work():
            meta = core.analyze_gcode(self.selected_file)
            ui(lambda: self._start_monitor(meta))
        threading.Thread(target=work, daemon=True).start()

    def _start_monitor(self, meta):
        n_layers = meta["max_layer"] + 1
        self.viz.set_data(meta["segments"])
        self._milestones = set()
        self._last_snap = time.time()

        def on_update(idx, layer, pct, temps, state):
            cap = f"Флешка: {pct:.1f}% · слой {layer + 1}/{n_layers}" + (f" · {temps}" if temps else "")
            ui(lambda: (self.viz.progress(idx, layer), setattr(self.viz_lbl, "text", cap)))
            for mark in (25, 50, 75):
                if pct >= mark and mark not in self._milestones:
                    self._milestones.add(mark)
                    self.tg.send(f"📈 Флешка: {mark}% · слой {layer + 1}/{n_layers}", log=self._log_plain)
            if self.tg.enabled and time.time() - self._last_snap >= core.SNAPSHOT_INTERVAL:
                self._last_snap = time.time()
                segs = meta["segments"]
                def snap(idx=idx, layer=layer, pct=pct):
                    png = core.render_snapshot_png(segs, idx, layer)
                    self.tg.send_photo(png, f"🖼 Флешка: {pct:.0f}% · слой {layer + 1}/{n_layers}",
                                       log=self._log_plain)
                threading.Thread(target=snap, daemon=True).start()

        def on_done(reason):
            self.log(reason)
            self.tg.send(("✅ " if "заверш" in reason else "ℹ️ ") + reason, log=self._log_plain)
            ui(lambda: setattr(self.mon_btn, "text", "👁 Мониторить печать с флешки"))

        self.sd_monitor = core.SDMonitor(self.link, meta, on_update, self.log, on_done)
        self.sd_monitor.start()
        self.mon_btn.text = "⏹ Остановить мониторинг"
        self.log(f"Мониторинг запущен: {len(meta['lines'])} строк, {n_layers} слоёв.")

    # ---------- telegram ----------
    def on_tg_toggle(self):
        self.tg_enabled = not self.tg_enabled
        self.tg_btn.text = "Уведомления: ВКЛ" if self.tg_enabled else "Уведомления: выкл"
        self._tg_apply()

    def on_tg_test(self):
        self._tg_apply()
        def work():
            ok, msg = self.tg.test()
            self.log(("Telegram: " if ok else "Telegram, ошибка: ") + msg)
        threading.Thread(target=work, daemon=True).start()

    def on_tg_find(self):
        self._tg_apply()
        def work():
            chat_id, info = self.tg.find_chat_id()
            def done():
                if chat_id:
                    self.tg_chat_in.text = chat_id
                    self._tg_apply()
                    self.log(f"Telegram: найден chat ID {chat_id} ({info}).")
                else:
                    self.log(f"Telegram: {info}")
            ui(done)
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    Ghost6App().run()
