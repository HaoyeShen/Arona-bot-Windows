"""Application settings and data maintenance."""

from __future__ import annotations

import json
import os
import shutil
import tkinter as tk
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from ... import __version__
from ...defaults import DEFAULT_CONFIG
from ...paths import BACKUP_DIR, CONFIG_FILE, DATA, DATABASE, LOGS, PERSONAS_FILE, STICKERS_FILE
from ...ui.theme import C, display_font, font, px
from ...ui.widgets import (
    Card,
    LabeledEntry,
    RoundedButton,
    ScrollFrame,
    SectionHeading,
    ToggleRow,
    confirm,
)
from .base import BasePage


class SettingsPage(BasePage):
    key = "settings"
    title = "设置"
    subtitle = "调整节奏、隐私与连接方式，让阿洛娜更贴合老师的使用习惯。"

    def __init__(self, parent, app) -> None:
        super().__init__(parent, app)
        self._vars: dict[str, tk.Variable] = {}
        self._build()
        self._load()

    def _var(self, key: str, value, kind=tk.StringVar) -> tk.Variable:
        var = kind(value=value)
        self._vars[key] = var
        return var

    def _build(self) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        wrap = tk.Frame(scroll.inner, bg=C["bg"])
        wrap.pack(fill="both", expand=True, padx=(0, px(4)), pady=(0, px(20)))

        top = tk.Frame(wrap, bg=C["bg"])
        top.pack(fill="x", pady=(0, px(12)))
        tk.Label(top, text="CONFIGURATION", bg=C["bg"], fg=C["cyan_deep"],
                 font=display_font(7, "bold")).pack(side="left", pady=(px(8), 0))
        self.save_btn = RoundedButton(top, text="保存设置", command=self.save,
                                      style="primary", width=112, height=34,
                                      bg=C["bg"], font_size=9)
        self.save_btn.pack(side="right")
        self.status = tk.Label(top, text="修改后请点击保存", bg=C["bg"], fg=C["muted"],
                               font=font(8))
        self.status.pack(side="right", padx=(0, px(9)), pady=(px(8), 0))

        columns = tk.Frame(wrap, bg=C["bg"])
        columns.pack(fill="both", expand=True)
        columns.grid_columnconfigure(0, weight=1, uniform="settings")
        columns.grid_columnconfigure(1, weight=1, uniform="settings")
        left = tk.Frame(columns, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(7)))
        right = tk.Frame(columns, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(px(7), 0))

        reply = Card(left, padding=16, accent=C["cyan"])
        reply.pack(fill="x")
        SectionHeading(reply.body, "回复行为", "怎么回、什么时候回").pack(fill="x", pady=(0, px(10)))
        self.auto_reply = ToggleRow(reply.body, "自动回复",
                                     "关闭后仍会记录消息与上下文，但不会发回微信。",
                                     self.config.bot.get("auto_reply", True))
        self.auto_reply.pack(fill="x", pady=px(5))
        self.auto_start = ToggleRow(reply.body, "启动 ARONA 时自动连接",
                                     "打开程序后自动开始监听，无需再点「启动机器人」。",
                                     self.config.bot.get("auto_start", True))
        self.auto_start.pack(fill="x", pady=px(5))
        self.auto_sticker = ToggleRow(reply.body, "允许使用表情包",
                                       "阿洛娜会结合语境从已启用的表情包里语义选择。",
                                       self.config.bot.get("auto_sticker", True))
        self.auto_sticker.pack(fill="x", pady=px(5))
        self.quiet = ToggleRow(reply.body, "免打扰时段",
                                "该时段内只记录消息，不自动回复。",
                                self.config.bot.get("quiet_hours_enabled", False))
        self.quiet.pack(fill="x", pady=px(5))

        quiet_times = tk.Frame(reply.body, bg=C["card"])
        quiet_times.pack(fill="x", pady=(px(4), px(8)))
        self.quiet_start = LabeledEntry(quiet_times, "开始", self.config.bot.get("quiet_start", "23:30"),
                                        hint="HH:MM", width=8)
        self.quiet_start.pack(side="left", fill="x", expand=True, padx=(0, px(5)))
        self.quiet_end = LabeledEntry(quiet_times, "结束", self.config.bot.get("quiet_end", "07:30"),
                                      hint="HH:MM", width=8)
        self.quiet_end.pack(side="left", fill="x", expand=True, padx=(px(5), 0))

        group_frame = tk.Frame(reply.body, bg=C["card"])
        group_frame.pack(fill="x", pady=(px(4), 0))
        self.group_at_always = ToggleRow(
            group_frame, "群聊 @ 后必回复",
            "被 @ 时不管概率多少，都会回复。",
            self.config.bot.get("group_at_always_reply", True),
        )
        self.group_at_always.pack(fill="x")
        self.group_probability = ToggleRow(
            group_frame, "群聊未 @ 概率回复",
            "没被 @ 时按下面的概率决定是否回复；关闭后未 @ 一律不回复。",
            self.config.bot.get("group_probability_reply", True),
        )
        self.group_probability.pack(fill="x")
        probability_row = tk.Frame(group_frame, bg=C["card"])
        probability_row.pack(fill="x", pady=(px(4), 0))
        self.group_probability_value = LabeledEntry(
            probability_row, "未 @ 回复概率（%）",
            str(round(float(self.config.bot.get("group_reply_probability", 0.3)) * 100)),
            hint="0–100", width=8,
        )
        self.group_probability_value.pack(fill="x")

        context = Card(left, padding=16)
        context.pack(fill="x", pady=(px(12), 0))
        SectionHeading(context.body, "上下文与节奏", "让回复更像真人").pack(fill="x", pady=(0, px(10)))
        grid = tk.Frame(context.body, bg=C["card"])
        grid.pack(fill="x")
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1)
        self.context_limit = LabeledEntry(grid, "上下文条数", str(self.config.bot.get("context_limit", 30)),
                                          hint="至少 20", width=8)
        self.context_limit.grid(row=0, column=0, sticky="ew", padx=(0, px(5)))
        self.max_chars = LabeledEntry(grid, "整条最多字", str(self.config.bot.get("max_reply_chars", 23)),
                                      width=8)
        self.max_chars.grid(row=0, column=1, sticky="ew", padx=px(5))
        self.sentence_chars = LabeledEntry(grid, "单句最多字", str(self.config.bot.get("max_sentence_chars", 23)),
                                           hint="23", width=8)
        self.sentence_chars.grid(row=0, column=2, sticky="ew", padx=px(5))
        self.max_stickers = LabeledEntry(grid, "每条最多表情", str(self.config.bot.get("max_stickers_per_reply", 1)),
                                         width=8)
        self.max_stickers.grid(row=0, column=3, sticky="ew", padx=(px(5), 0))
        grid2 = tk.Frame(context.body, bg=C["card"])
        grid2.pack(fill="x", pady=(px(8), 0))
        for i in range(3):
            grid2.grid_columnconfigure(i, weight=1)
        self.delay_min = LabeledEntry(grid2, "最短等待（秒）", str(self.config.bot.get("reply_delay_min", 1.0)),
                                      width=8)
        self.delay_min.grid(row=0, column=0, sticky="ew", padx=(0, px(5)))
        self.delay_max = LabeledEntry(grid2, "最长等待（秒）", str(self.config.bot.get("reply_delay_max", 2.8)),
                                      width=8)
        self.delay_max.grid(row=0, column=1, sticky="ew", padx=px(5))
        self.cooldown = LabeledEntry(grid2, "单会话冷却", str(self.config.bot.get("per_chat_cooldown", 6.0)),
                                     hint="秒", width=8)
        self.cooldown.grid(row=0, column=2, sticky="ew", padx=(px(5), 0))
        self.sticker_prob = LabeledEntry(context.body, "主动追加表情包概率",
                                          str(self.config.bot.get("sticker_probability", 0.35)),
                                          hint="0–1 · 模型未主动选择时", width=12)
        self.sticker_prob.pack(fill="x", pady=(px(8), 0))
        tk.Label(context.body, text="修改上下文条数不会删除已有记录，只影响每次发送给模型的历史长度。",
                 bg=C["card"], fg=C["muted"], font=font(7), anchor="w").pack(fill="x", pady=(px(6), 0))

        wechat = Card(right, padding=16, accent=C["pink"])
        wechat.pack(fill="x")
        SectionHeading(wechat.body, "微信连接", "UI Automation 通道").pack(fill="x", pady=(0, px(10)))
        backend_row = tk.Frame(wechat.body, bg=C["card"])
        backend_row.pack(fill="x", pady=(0, px(8)))
        tk.Label(backend_row, text="适配器", bg=C["card"], fg=C["text_soft"],
                 font=font(9, "bold")).pack(anchor="w", pady=(0, px(4)))
        self.backend = ttk.Combobox(backend_row, values=["自动选择",
                                                         "本地数据库 + UIA（4.1.15）"],
                                    state="readonly", style="Arona.TCombobox", font=font(9))
        self.backend.pack(fill="x")
        self.self_name = LabeledEntry(wechat.body, "我的微信昵称（可选）",
                                       self.config.wechat.get("self_name", ""),
                                       hint="群聊 @ 判断辅助", width=25)
        self.self_name.pack(fill="x", pady=(px(2), px(8)))
        tk.Label(wechat.body,
                 text="程序不使用 wxauto。当前只保留本地数据库 + UIA 路线，"
                      "只监听白名单会话；OCR/截图调用已关闭，图片通过 UIA 剪贴板发送。",
                 bg=C["card"], fg=C["muted"], font=font(8), anchor="w", justify="left",
                 wraplength=px(420)).pack(fill="x")
        restart_note = tk.Label(wechat.body, text="切换适配器后需要重启机器人生效。",
                                bg=C["card"], fg=C["yellow_deep"], font=font(7), anchor="w")
        restart_note.pack(fill="x", pady=(px(5), 0))
        tk.Label(wechat.body,
                 text="本地数据库 + UIA（4.1.15）：读取本机微信数据库做监听，"
                      "并在微信进程中临时激活 Qt 无障碍位用于 UIA 发送；OCR/截图已关闭。"
                      "仅限本人设备、本人账号使用。",
                 bg=C["card"], fg=C["muted"], font=font(7), justify="left",
                 anchor="w", wraplength=px(420)).pack(fill="x", pady=(px(3), 0))

        admin_card = Card(right, padding=16, accent=C["cyan"])
        admin_card.pack(fill="x", pady=(px(12), 0))
        SectionHeading(admin_card.body, "管理员指令", "只接受正确密码的发送者").pack(
            fill="x", pady=(0, px(10))
        )
        self.admin_password = LabeledEntry(
            admin_card.body, "管理员密码",
            str(self.config.data.get("admin", {}).get("password", "arona-admin")),
            hint="发送 /admin 后输入", width=24,
        )
        self.admin_password.pack(fill="x", pady=(0, px(6)))
        tk.Label(
            admin_card.body,
            text="命令：/admin 验证 · /reset 清空当前上下文 · /test 调试信息 · "
                 "/off 暂停一小时 · /start 恢复 · /end 保存并退出。\n"
                 "管理员模式下会暂停该会话的智能回复与 API 调用；只有验证通过的发送者能下指令。",
            bg=C["card"], fg=C["muted"], font=font(7), justify="left", anchor="w",
            wraplength=px(420),
        ).pack(fill="x", pady=(px(4), 0))

        privacy = Card(right, padding=16)
        privacy.pack(fill="x", pady=(px(12), 0))
        SectionHeading(privacy.body, "隐私与日志", "数据留在本机").pack(fill="x", pady=(0, px(10)))
        self.log_content = ToggleRow(privacy.body, "在日志中记录消息摘要",
                                      "关闭后日志只显示会话与长度，适合共享屏幕时使用。",
                                      self.config.bot.get("log_message_content", True))
        self.log_content.pack(fill="x", pady=px(5))
        self.confirm_close = ToggleRow(privacy.body, "退出时二次确认",
                                        "避免误关正在运行的机器人。",
                                        self.config.ui.get("confirm_close", True))
        self.confirm_close.pack(fill="x", pady=px(5))
        tk.Label(privacy.body,
                 text="上下文与回复仅写入本机 data/arona.db；调用第三方 API 时，最近的对话会发送给该服务商。",
                 bg=C["card"], fg=C["muted"], font=font(8), justify="left", anchor="w",
                 wraplength=px(420)).pack(fill="x", pady=(px(8), 0))

        data_card = Card(right, padding=16)
        data_card.pack(fill="x", pady=(px(12), 0))
        SectionHeading(data_card.body, "数据维护", "备份与清理").pack(fill="x", pady=(0, px(10)))
        buttons = tk.Frame(data_card.body, bg=C["card"])
        buttons.pack(fill="x")
        RoundedButton(buttons, text="备份数据", command=self.backup, style="secondary",
                      width=84, height=30, bg=C["card"], font_size=8).pack(side="left")
        RoundedButton(buttons, text="打开数据文件夹", command=self.open_data, style="ghost",
                      width=102, height=30, bg=C["card"], font_size=8).pack(side="left",
                                                                             padx=px(4))
        RoundedButton(buttons, text="导出无 Key 配置", command=self.export_config, style="ghost",
                      width=104, height=30, bg=C["card"], font_size=8).pack(side="left")
        RoundedButton(buttons, text="清空全部上下文", command=self.clear_context, style="danger",
                      width=102, height=30, bg=C["card"], font_size=8).pack(side="right")

        about = Card(wrap, padding=16, bg=C["sidebar"], border=False)
        about.pack(fill="x", pady=(px(12), 0))
        tk.Label(about.body, text=f"ARONA  v{__version__}", bg=C["sidebar"], fg="white",
                 font=display_font(11, "bold"), anchor="w").pack(fill="x")
        tk.Label(about.body,
                 text="什亭之匣桌面助手 · 仅使用 Windows UI Automation，不含截图、录屏与 Codex 依赖。\n"
                      "非腾讯官方产品。请仅对你本人有权操作的微信会话使用，并遵守平台规则。",
                 bg=C["sidebar"], fg=C["nav_text"], font=font(8), justify="left",
                 anchor="w", wraplength=px(900)).pack(fill="x", pady=(px(5), 0))

    # ------------------------------------------------------------------
    def _load(self) -> None:
        mapping = {"auto": "自动选择",
                   "wechatauto": "本地数据库 + UIA（4.1.15）"}
        self.backend.set(mapping.get(self.config.wechat.get("backend", "auto"), "自动选择"))

    def on_show(self) -> None:
        self._load()

    def save(self) -> None:
        try:
            context_limit = max(20, min(100, int(self.context_limit.get())))
            max_chars = max(8, min(1800, int(self.max_chars.get() or 23)))
            sentence_chars = max(8, min(80, int(self.sentence_chars.get() or 23)))
            group_probability = max(0, min(100, int(float(self.group_probability_value.get() or 30))))
            max_stickers = max(0, min(3, int(self.max_stickers.get())))
            delay_min = max(0.0, min(30.0, float(self.delay_min.get())))
            delay_max = max(delay_min, min(30.0, float(self.delay_max.get())))
            cooldown = max(0.0, min(600.0, float(self.cooldown.get())))
            probability = max(0.0, min(1.0, float(self.sticker_prob.get())))
            if self.quiet.get():
                if not self.quiet_start.get() or not self.quiet_end.get():
                    raise ValueError("免打扰时段不能为空")
            for time_value in (self.quiet_start.get(), self.quiet_end.get()):
                if time_value:
                    datetime.strptime(time_value, "%H:%M")
        except ValueError as exc:
            self.app.toast("设置格式不正确：请检查数字与时间（HH:MM）。", "error")
            return
        backend_before = self.config.wechat.get("backend", "auto")
        bot = self.config.bot
        bot.update({
            "auto_reply": self.auto_reply.get(),
            "auto_start": self.auto_start.get(),
            "auto_sticker": self.auto_sticker.get(),
            "quiet_hours_enabled": self.quiet.get(),
            "quiet_start": self.quiet_start.get(),
            "quiet_end": self.quiet_end.get(),
            "context_limit": context_limit,
            "max_reply_chars": max_chars,
            "max_sentence_chars": sentence_chars,
            "group_at_always_reply": self.group_at_always.get(),
            "group_probability_reply": self.group_probability.get(),
            "group_reply_probability": group_probability / 100.0,
            "max_stickers_per_reply": max_stickers,
            "reply_delay_min": delay_min,
            "reply_delay_max": delay_max,
            "per_chat_cooldown": cooldown,
            "sticker_probability": probability,
            "log_message_content": self.log_content.get(),
        })
        self.config.wechat.update({
            "backend": {"自动选择": "auto",
                        "本地数据库 + UIA（4.1.15）": "wechatauto"}.get(
                            self.backend.get(), "auto"),
            "self_name": self.self_name.get(),
        })
        self.config.ui["confirm_close"] = self.confirm_close.get()
        admin_password = self.admin_password.get().strip()
        if not admin_password:
            self.app.toast("管理员密码不能为空。", "error")
            return
        self.config.data.setdefault("admin", {})["password"] = admin_password
        self.config.save()
        self.service.reload_config()
        self.status.configure(text="已保存 · " + datetime.now().strftime("%H:%M:%S"),
                              fg=C["green"])
        self.app.toast("设置已保存。", "success")
        self.app.refresh_header()
        if backend_before != self.config.wechat.get("backend") and self.service.running:
            if confirm(self.winfo_toplevel(), "重启机器人",
                       "微信适配器已更改，需要重启机器人才能生效。现在重启吗？",
                       ok_text="立即重启", icon=self.icon):
                self.app.restart_bot()

    def clear_context(self) -> None:
        if not confirm(self.winfo_toplevel(), "清空全部上下文",
                       "这会删除本机数据库中所有对话与回复记录，包括调试台历史。\n"
                       "此操作不可撤销，但不会影响微信聊天记录。",
                       ok_text="清空", danger=True, icon=self.icon):
            return
        count = self.memory.clear()
        self.app.toast(f"已清空 {count} 条本地记录。", "success")
        self.app.refresh_header()

    def backup(self) -> None:
        snapshot = BACKUP_DIR / ".arona-snapshot.db"
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = BACKUP_DIR / f"arona-backup-{stamp}.zip"
            # SQLite's online backup API gives a consistent DB snapshot even
            # while the bot is running.
            import sqlite3
            source = sqlite3.connect(str(DATABASE))
            dest = sqlite3.connect(str(snapshot))
            try:
                source.backup(dest)
            finally:
                dest.close()
                source.close()
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in (CONFIG_FILE, PERSONAS_FILE, STICKERS_FILE):
                    if path.exists():
                        archive.write(path, path.relative_to(DATA))
                if snapshot.exists():
                    archive.write(snapshot, "arona.db")
                sticker_dir = DATA / "stickers"
                for image in sticker_dir.glob("*"):
                    if image.is_file():
                        archive.write(image, image.relative_to(DATA))
            self.app.toast(f"备份已保存：{target.name}", "success")
        except Exception as exc:
            self.app.toast("备份失败：" + str(exc), "error")
        finally:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass

    def export_config(self) -> None:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = BACKUP_DIR / f"arona-config-public-{stamp}.json"
            target.write_text(json.dumps(self.config.export_public(), ensure_ascii=False,
                                         indent=2), encoding="utf-8")
            self.app.toast(f"已导出（不含 Key）：{target.name}", "success")
        except Exception as exc:
            self.app.toast("导出失败：" + str(exc), "error")

    def open_data(self) -> None:
        try:
            os.startfile(str(DATA))
        except Exception as exc:
            self.app.toast("无法打开数据文件夹：" + str(exc), "error")
