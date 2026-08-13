"""
BlueStacks root CA installer — Python port of guna2Button2_Click / guna2Button3_Click (Form1.cs).
Connects to a BlueStacks ADB instance and installs/removes c8750f0d.0 in the
Android system trust store via su. Every adb/su command and its raw output is
logged so a failure can be diagnosed from the log pane.
"""
import re
import subprocess
import threading
import winreg
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
import socket
from typing import Optional, List
import customtkinter as ctk

CERT_HASH = "c8750f0d"
CERT_PATH = Path(__file__).resolve().parent / f"{CERT_HASH}.0"
DEVICE_CERT_PATH = f"/system/etc/security/cacerts/{CERT_HASH}.0"
SU_PATH = "/boot/android/android/system/xbin/bstk/su"

ADB_PATHS = {
    "Bluestacks": r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
    "Msi": r"C:\Program Files\Bluestacks_msi5\HD-Adb.exe",
}
PLAYER_PATHS = {
    "Bluestacks": r"C:\Program Files\BlueStacks_nxt\HD-Player.exe",
    "Msi": r"C:\Program Files\Bluestacks_msi5\HD-Player.exe",
}
# HKLM\SOFTWARE\<key>\DataDir holds the real engine root — installs on a
# custom drive (UserDefinedDir) make the C:\ProgramData default wrong.
ENGINE_REGISTRY_KEYS = {"Bluestacks": "BlueStacks_nxt", "Msi": "BlueStacks_msi5"}
ENGINE_FALLBACK = {
    "Bluestacks": r"C:\ProgramData\BlueStacks_nxt\Engine",
    "Msi": r"C:\ProgramData\Bluestacks_msi5\Engine",
}
PROCESSES_TO_KILL = ["HD-Player", "HD-Adb", "HD-MultiInstanceManager", "BstkSVC"]

CREATE_NO_WINDOW = 0x08000000

# Substrings in adb/su output that explain a failure in plain English.
FAILURE_HINTS = [
    ("no devices/emulators found", "No device on that port — is the BlueStacks instance running?"),
    ("cannot connect", "Emulator refused the connection — check the port matches the instance."),
    ("cannot stat", "Local file path is wrong or missing — check CERT_PATH exists."),
    ("not found", "su binary missing at this path — this BlueStacks build may not be rooted, or the path changed."),
    ("permission denied", "su did not grant root — root access may be disabled in BlueStacks settings."),
    ("read-only", "/system's backing Root.vhd is opened read-only by the BlueStacks engine itself "
                  "(set in its .bstk config) — no in-guest su/mount command can override that. "
                  "Click 'Unlock /system & Restart Emulator', wait for it to boot, then retry."),
    ("no such file or directory", "A path in the command chain doesn't exist on this device."),
]


def resolve_adb_path(variant: str) -> str:
    path = ADB_PATHS.get(variant)
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"No HD-Adb.exe found for '{variant}' at {path}")
    return path


def run_adb(adb_exe: str, port: str, argv: list, log) -> str:
    log(">> adb " + " ".join(argv))
    result = subprocess.run(
        [adb_exe, "-s", f"127.0.0.1:{port}", *argv],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    output = (result.stdout + result.stderr).strip()
    log(output if output else "(no output)")

    lower = output.lower()
    for needle, hint in FAILURE_HINTS:
        if needle in lower:
            log(f"   -> likely cause: {hint}")
            break
    return output


def connect_adb(adb_exe: str, port: str, log) -> bool:
    output = run_adb(adb_exe, port, ["connect", f"127.0.0.1:{port}"], log)
    return "connected" in output


def install_cert(adb_exe: str, port: str, log) -> bool:
    push_output = run_adb(adb_exe, port, ["push", str(CERT_PATH), f"/sdcard/{CERT_HASH}.0"], log)
    if "error" in push_output.lower():
        log("FAILED: push did not complete — aborting before touching /system.")
        return False

    cmd = (
        f"{SU_PATH} -c 'mount -o rw,remount /dev/sda1 /system && "
        f"cp /sdcard/{CERT_HASH}.0 {DEVICE_CERT_PATH} && "
        f"chmod 644 {DEVICE_CERT_PATH} && "
        f"chcon u:object_r:system_file:s0 {DEVICE_CERT_PATH} && "
        f"mount -o ro,remount /dev/sda1 /system && "
        f"rm /sdcard/{CERT_HASH}.0 && "
        f"setprop ctl.restart zygote'"
    )
    run_adb(adb_exe, port, ["shell", cmd], log)

    check = f"[ -f {DEVICE_CERT_PATH} ] && echo yes || echo no"
    output = run_adb(adb_exe, port, ["shell", check], log)
    return output.strip().lower() == "yes"


def get_engine_root(variant: str) -> Path:
    reg_key = ENGINE_REGISTRY_KEYS.get(variant)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"SOFTWARE\\{reg_key}") as key:
            data_dir, _ = winreg.QueryValueEx(key, "DataDir")
        p = Path(data_dir)
        # Some installs store the DataDir as the Engine path already.
        return p if p.name.lower() == "engine" else p / "Engine"
    except OSError:
        fb = Path(ENGINE_FALLBACK[variant])
        return fb if fb.name.lower() == "engine" else fb / "Engine"


def kill_bluestacks_processes(log):
    for name in PROCESSES_TO_KILL:
        log(f">> taskkill /IM {name}.exe /F")
        result = subprocess.run(
            ["taskkill", "/IM", f"{name}.exe", "/F"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        output = (result.stdout + result.stderr).strip()
        log(output if output else "(no output)")


# Flip <HardDisk ... location="Root.vhd"|"Data.vhdx" ... type="Readonly"/> to Normal
# so the BlueStacks engine opens the disk read-write on next launch.
_READONLY_PATTERNS = [
    re.compile(r'(<HardDisk\b[^>]*location\s*=\s*"Root\.vhd"[^>]*type\s*=\s*")Readonly("\s*/?>)', re.IGNORECASE),
    re.compile(r'(<HardDisk\b[^>]*location\s*=\s*"Data\.vhdx"[^>]*type\s*=\s*")Readonly("\s*/?>)', re.IGNORECASE),
]


def patch_readonly_disks(engine_root: Path, log, instance_name: Optional[str] = None) -> List[Path]:
    if not engine_root.exists():
        raise FileNotFoundError(f"Engine directory not found: {engine_root}")

    patched: List[Path] = []
    instances = []
    if instance_name:
        inst = engine_root / instance_name
        if inst.exists() and inst.is_dir():
            instances = [inst]
        else:
            log(f"Selected instance not found: {inst}")
            return patched
    else:
        instances = [d for d in engine_root.iterdir() if d.is_dir()]

    for instance_dir in instances:
        candidates = [instance_dir / "Android.bstk.in",
                      instance_dir / f"{instance_dir.name}.bstk",
                      instance_dir / f"{instance_dir.name}.bstk-prev"]
        for f in candidates:
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8")
            new_text = text
            for pattern in _READONLY_PATTERNS:
                new_text = pattern.sub(r"\1Normal\2", new_text)
            if new_text != text:
                f.write_text(new_text, encoding="utf-8")
                patched.append(f)
                log(f"patched Readonly -> Normal: {f}")
    return patched


def delete_bstk_logs(engine_root: Path, log, instance_name: Optional[str] = None) -> List[Path]:
    deleted: List[Path] = []
    targets = ["BstkServer.log", "BstkCore.log"]
    bases: List[Path] = []
    if instance_name:
        inst = engine_root / instance_name
        if inst.exists() and inst.is_dir():
            bases = [inst]
        else:
            log(f"Selected instance not found for log deletion: {inst}")
            return deleted
    else:
        bases = [engine_root] + [p for p in engine_root.iterdir() if p.is_dir()]

    for base in bases:
        for name in targets:
            path = base / name
            if path.exists():
                try:
                    path.unlink()
                    deleted.append(path)
                    log(f"Deleted log file: {path}")
                except Exception as exc:
                    log(f"Failed to delete {path}: {exc}")
    if not deleted:
        log("No BstkServer.log or BstkCore.log files found to delete.")
    return deleted


def unlock_system_disk(variant: str, log, instance_name: Optional[str] = None):
    log("=== Killing BlueStacks processes (releases the VHD file locks) ===")
    kill_bluestacks_processes(log)

    engine_root = get_engine_root(variant)
    log(f"Engine root: {engine_root}")
    patched = patch_readonly_disks(engine_root, log, instance_name)
    if not patched:
        log("No Readonly HardDisk entries found — /system may already be writable, "
            "or this variant's config lives elsewhere.")

    delete_bstk_logs(engine_root, log, instance_name)

    player = PLAYER_PATHS.get(variant)
    if player and Path(player).exists():
        subprocess.Popen([player])
        log(f"Relaunched {player} — wait for the emulator to finish booting before installing.")
    else:
        log(f"Could not find HD-Player.exe at {player} — start the emulator manually.")
    return patched


def uninstall_cert(adb_exe: str, port: str, log) -> bool:
    cmd = (
        f"{SU_PATH} -c 'mount -o rw,remount /dev/sda1 /system && "
        f"rm {DEVICE_CERT_PATH} && "
        f"mount -o ro,remount /dev/sda1 /system &'"
    )
    run_adb(adb_exe, port, ["shell", cmd], log)

    check = f"[ -f {DEVICE_CERT_PATH} ] && echo yes || echo no"
    output = run_adb(adb_exe, port, ["shell", check], log)
    return output.strip().lower() == "no"


def set_device_proxy(adb_exe: str, port: str, host: str, proxy_port: str, log) -> bool:
    # Try multiple settings keys to maximize compatibility
    cmds = [
        f"settings put global http_proxy {host}:{proxy_port}",
        f"settings put global global_http_proxy_host {host}",
        f"settings put global global_http_proxy_port {proxy_port}",
    ]
    success = True
    for c in cmds:
        out = run_adb(adb_exe, port, ["shell", c], log)
        if "error" in out.lower() or "not found" in out.lower():
            success = False

    if success:
        log(f"Proxy set to {host}:{proxy_port} on device 127.0.0.1:{port}")
        return True

    # Fallback: attempt to run the same commands via the in-guest su binary
    log("Direct settings failed — attempting su fallback to set proxy inside guest")
    su_cmd = (
        f"{SU_PATH} -c 'settings put global http_proxy {host}:{proxy_port} && "
        f"settings put global global_http_proxy_host {host} && "
        f"settings put global global_http_proxy_port {proxy_port} && "
        f"setprop ctl.restart zygote'"
    )
    out = run_adb(adb_exe, port, ["shell", su_cmd], log)
    if "error" in out.lower() or "not found" in out.lower():
        log("SU fallback failed to set proxy — device may not be rooted or su path is wrong.")
        return False
    log(f"Proxy set via su to {host}:{proxy_port} on device 127.0.0.1:{port}")
    return True


def clear_device_proxy(adb_exe: str, port: str, log) -> bool:
    cmds = [
        "settings put global http_proxy :0",
        'settings put global global_http_proxy_host ""',
        "settings put global global_http_proxy_port 0",
    ]
    success = True
    for c in cmds:
        out = run_adb(adb_exe, port, ["shell", c], log)
        if "error" in out.lower() or "not found" in out.lower():
            success = False

    if success:
        log(f"Cleared proxy on device 127.0.0.1:{port}")
        return True

    # Fallback via su inside guest
    log("Direct clear failed — attempting su fallback to clear proxy inside guest")
    su_cmd = (
        f"{SU_PATH} -c 'settings put global http_proxy :0 && "
        f"settings put global global_http_proxy_host \"\" && "
        f"settings put global global_http_proxy_port 0 && "
        f"setprop ctl.restart zygote'"
    )
    out = run_adb(adb_exe, port, ["shell", su_cmd], log)
    if "error" in out.lower() or "not found" in out.lower():
        log("SU fallback failed to clear proxy — device may not be rooted or su path is wrong.")
        return False
    log(f"Cleared proxy via su on device 127.0.0.1:{port}")
    return True


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Emulator Root Certificate Installer v1.0.0")
        self.geometry("720x520")
        self.minsize(620, 400)

        MONO = ("Consolas", 10)

        # Create tabbed UI: Patch | Install Certificate | Proxy | Logs
        tabview = ctk.CTkTabview(self)
        tabview.pack(fill="both", expand=True, padx=12, pady=12)
        patch_tab = tabview.add("Patch")
        cert_tab = tabview.add("Install Certificate")
        proxy_tab = tabview.add("Proxy")
        logs_tab = tabview.add("Logs")

        # Patch tab (unlock /system, engine selection)
        ctk.CTkLabel(patch_tab, text="ADB variant:").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.variant = tk.StringVar(value=self._default_variant())
        options = [v for v, p in ADB_PATHS.items() if Path(p).exists()] or list(ADB_PATHS)
        self.variant_combo = ctk.CTkComboBox(patch_tab, variable=self.variant, values=options, state="readonly",
                                              command=lambda choice: self._populate_engine_instances())
        self.variant_combo.grid(row=0, column=1, padx=6, pady=8)

        ctk.CTkLabel(patch_tab, text="Engine instance:").grid(row=1, column=0, padx=6, pady=8, sticky="w")
        self.engine_instance = tk.StringVar(value="")
        self.engine_combo = ctk.CTkComboBox(patch_tab, variable=self.engine_instance, values=[], state="readonly")
        self.engine_combo.grid(row=1, column=1, padx=6, pady=8)
        self._populate_engine_instances()

        ctk.CTkLabel(patch_tab, text="ADB Port:").grid(row=0, column=2, padx=(16, 6), pady=8, sticky="w")
        self.port = ctk.CTkEntry(patch_tab, width=70, justify="center")
        self.port.insert(0, "5555")
        self.port.grid(row=0, column=3, padx=6, pady=8)
        self.unlock_btn = ctk.CTkButton(patch_tab, text="Unlock /system + Restart Emulator", command=self.on_unlock)
        self.unlock_btn.grid(row=0, column=4, padx=6, pady=8)

        # Certificate tab
        ctk.CTkLabel(cert_tab, text="Paste a PEM certificate below, or load one from disk, then click Submit:").pack(
            anchor="w", padx=8, pady=(8, 0))
        self.cert_text = ctk.CTkTextbox(cert_tab, height=220, font=MONO)
        self.cert_text.pack(fill="both", expand=True, padx=8, pady=8)
        cert_btn_frame = ctk.CTkFrame(cert_tab, fg_color="transparent")
        cert_btn_frame.pack(pady=(0, 8))
        self.load_pem_btn = ctk.CTkButton(cert_btn_frame, text="Load .pem File…", command=self.on_load_pem_file,
                                           fg_color="transparent", border_width=1)
        self.load_pem_btn.grid(row=0, column=0, padx=6)
        self.submit_cert_btn = ctk.CTkButton(cert_btn_frame, text="Submit Certificate", command=self.on_submit_certificate)
        self.submit_cert_btn.grid(row=0, column=1, padx=6)
        self.install_btn = ctk.CTkButton(cert_btn_frame, text="Install Cert", command=self.on_install,
                                          fg_color="transparent", border_width=1)
        self.install_btn.grid(row=0, column=2, padx=6)
        self.uninstall_btn = ctk.CTkButton(cert_btn_frame, text="Uninstall Cert", command=self.on_uninstall,
                                            fg_color="transparent", border_width=1)
        self.uninstall_btn.grid(row=0, column=3, padx=6)

        # Proxy tab
        ctk.CTkLabel(proxy_tab, text="Proxy IP:").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.proxy_ip = ctk.CTkEntry(proxy_tab, width=140, justify="center")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "127.0.0.1"
        self.proxy_ip.insert(0, local_ip)
        self.proxy_ip.grid(row=0, column=1, padx=6, pady=8)
        ctk.CTkLabel(proxy_tab, text="Proxy Port:").grid(row=1, column=0, padx=6, pady=8, sticky="w")
        self.proxy_port = ctk.CTkEntry(proxy_tab, width=70, justify="center")
        self.proxy_port.insert(0, "6969")
        self.proxy_port.grid(row=1, column=1, padx=6, pady=8)
        self.proxy_connect_btn = ctk.CTkButton(proxy_tab, text="Connect Proxy", command=self.on_proxy_connect)
        self.proxy_connect_btn.grid(row=0, column=2, padx=6, pady=8)
        self.proxy_disconnect_btn = ctk.CTkButton(proxy_tab, text="Disconnect Proxy", command=self.on_proxy_disconnect,
                                                   fg_color="transparent", border_width=1)
        self.proxy_disconnect_btn.grid(row=1, column=2, padx=6, pady=8)

        # Logs tab
        self.log_box = ctk.CTkTextbox(logs_tab, height=320, font=("Consolas", 9), state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)

        # Status label
        self.status = ctk.CTkLabel(self, text="Ready", text_color="gray")
        self.status.pack(pady=(0, 8))

    def _default_variant(self):
        for name, path in ADB_PATHS.items():
            if Path(path).exists():
                return name
        return "Bluestacks"

    def _populate_engine_instances(self):
        try:
            engine_root = get_engine_root(self.variant.get())
        except Exception as e:
            self.log(f"Could not read engine root: {e}")
            engine_root = None

        options = []
        if engine_root and engine_root.exists():
            try:
                options = [p.name for p in engine_root.iterdir() if p.is_dir()]
            except Exception as e:
                self.log(f"Failed listing engine instances: {e}")

        # populate the combobox with discovered instances
        if options:
            self.engine_combo.configure(values=options)
            if not self.engine_instance.get():
                self.engine_instance.set(options[0])
        else:
            self.engine_combo.configure(values=[])
            self.engine_instance.set("")

    def set_status(self, text: str, color: str = "gray"):
        self.after(0, lambda: self.status.configure(text=text, text_color=color))

    def log(self, line: str):
        self.after(0, lambda: self._append_log(line))

    def _append_log(self, line: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.install_btn.configure(state=state)
        self.uninstall_btn.configure(state=state)
        self.unlock_btn.configure(state=state)
        # proxy buttons
        try:
            self.proxy_connect_btn.configure(state=state)
            self.proxy_disconnect_btn.configure(state=state)
        except Exception:
            pass
        # certificate submit button
        try:
            self.submit_cert_btn.configure(state=state)
        except Exception:
            pass

    def on_install(self):
        self._set_buttons(False)
        threading.Thread(target=self._run, args=(self._install_worker,), daemon=True).start()

    def on_uninstall(self):
        self._set_buttons(False)
        threading.Thread(target=self._run, args=(self._uninstall_worker,), daemon=True).start()

    def on_unlock(self):
        self._set_buttons(False)
        threading.Thread(target=self._run, args=(self._unlock_worker,), daemon=True).start()

    def on_proxy_connect(self):
        self._set_buttons(False)
        threading.Thread(target=self._run, args=(self._proxy_connect_worker,), daemon=True).start()

    def on_proxy_disconnect(self):
        self._set_buttons(False)
        threading.Thread(target=self._run, args=(self._proxy_disconnect_worker,), daemon=True).start()

    def on_load_pem_file(self):
        path = filedialog.askopenfilename(
            title="Select PEM certificate",
            filetypes=[("PEM certificate", "*.pem")],
        )
        if not path:
            return
        # Dialog filter narrows the picker list, but doesn't stop a typed-in path —
        # enforce the .pem extension ourselves too.
        if Path(path).suffix.lower() != ".pem":
            self.log(f"Rejected file (not .pem): {path}")
            self.set_status("Only .pem files are allowed", "red")
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except Exception as e:
            self.log(f"Failed to read {path}: {e}")
            self.set_status(f"Load Failed ({e})", "red")
            return
        self.cert_text.delete("1.0", "end")
        self.cert_text.insert("1.0", text)
        self.log(f"Loaded certificate from {path}")
        self.set_status("Certificate loaded — click Submit to save", "green")

    def on_submit_certificate(self):
        text = self.cert_text.get("1.0", "end").strip()
        if not text:
            self.log("No certificate text provided to submit.")
            self.set_status("No certificate provided", "red")
            return
        try:
            CERT_PATH.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
            self.log(f"Written certificate to {CERT_PATH}")
            self.set_status("Certificate submitted", "green")
        except Exception as e:
            self.log(f"Failed to write certificate: {e}")
            self.set_status(f"Submit Failed ({e})", "red")

    def _run(self, worker):
        try:
            worker()
        finally:
            self.after(0, lambda: self._set_buttons(True))

    def _connect(self):
        adb = resolve_adb_path(self.variant.get())
        port = self.port.get().strip() or "5555"
        self.set_status("Connecting...", "gray")
        self.log(f"=== Connecting to 127.0.0.1:{port} via {adb} ===")
        if not connect_adb(adb, port, self.log):
            self.set_status("ADB Connection Failed! See log for details.", "red")
            self.log("FAILED: adb did not report 'connected'. Is the emulator running and the port correct?")
            return None, None
        self.set_status("ADB Connected!", "green")
        return adb, port

    def _proxy_connect_worker(self):
        self.log("\n----- Connect Proxy -----")
        try:
            adb, port = self._connect()
            if not adb:
                return
            ip = self.proxy_ip.get().strip()
            p = self.proxy_port.get().strip() or "6969"
            self.set_status("Setting proxy...", "gray")
            if set_device_proxy(adb, port, ip, p, self.log):
                self.set_status("Proxy Connected", "green")
            else:
                self.set_status("Proxy Connect Failed", "red")
        except Exception as e:
            self.set_status(f"Proxy Connect Failed! ({e})", "red")
            self.log(f"EXCEPTION: {type(e).__name__}: {e}")

    def _proxy_disconnect_worker(self):
        self.log("\n----- Disconnect Proxy -----")
        try:
            adb, port = self._connect()
            if not adb:
                return
            self.set_status("Clearing proxy...", "gray")
            if clear_device_proxy(adb, port, self.log):
                self.set_status("Proxy Disconnected", "#0090ff")
            else:
                self.set_status("Proxy Disconnect Failed", "red")
        except Exception as e:
            self.set_status(f"Proxy Disconnect Failed! ({e})", "red")
            self.log(f"EXCEPTION: {type(e).__name__}: {e}")

    def _install_worker(self):
        self.log("\n----- Install Cert -----")
        try:
            if not CERT_PATH.exists():
                raise FileNotFoundError(f"Cert not found next to script: {CERT_PATH}")

            adb, port = self._connect()
            if not adb:
                return

            self.set_status("Installing cert...", "gray")
            self.log("=== Pushing + installing cert into system trust store ===")
            if install_cert(adb, port, self.log):
                self.set_status("Cert Installed!", "green")
                self.log("SUCCESS: cert present at " + DEVICE_CERT_PATH)
                # remove the locally created certificate file after successful install
                try:
                    if CERT_PATH.exists():
                        CERT_PATH.unlink()
                        self.log(f"Deleted local certificate file: {CERT_PATH}")
                        try:
                            self.cert_text.delete("1.0", "end")
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f"Failed to delete local certificate: {e}")
            else:
                self.set_status("Install Failed! See log for details.", "red")
                self.log("FAILED: verification check did not find the cert on-device. "
                         "Review the su command output above for the step that errored "
                         "(su not found, mount refused, cp/chcon failed, etc).")
        except Exception as e:
            self.set_status(f"Install Failed! ({e})", "red")
            self.log(f"EXCEPTION: {type(e).__name__}: {e}")

    def _uninstall_worker(self):
        self.log("\n----- Uninstall Cert -----")
        try:
            adb, port = self._connect()
            if not adb:
                return

            self.set_status("Removing cert...", "gray")
            self.log("=== Removing cert from system trust store ===")
            if uninstall_cert(adb, port, self.log):
                self.set_status("Cert Removed!", "#0090ff")
                self.log("SUCCESS: cert no longer present at " + DEVICE_CERT_PATH)
            else:
                self.set_status("Remove Failed! See log for details.", "red")
                self.log("FAILED: cert still present after removal attempt. "
                         "Review the su command output above (su not found, mount refused, rm failed, etc).")
        except Exception as e:
            self.set_status(f"Remove Failed! ({e})", "red")
            self.log(f"EXCEPTION: {type(e).__name__}: {e}")

    def _unlock_worker(self):
        self.log("\n----- Unlock /system + Restart Emulator -----")
        try:
            variant = self.variant.get()
            instance = self.engine_instance.get() or None
            self.set_status("Unlocking /system...", "gray")
            patched = unlock_system_disk(variant, self.log, instance)
            if patched:
                self.set_status("Unlocked! Wait for emulator to boot, then retry.", "#0090ff")
            else:
                self.set_status("Nothing to patch — see log.", "orange")
        except Exception as e:
            self.set_status(f"Unlock Failed! ({e})", "red")
            self.log(f"EXCEPTION: {type(e).__name__}: {e}")


if __name__ == "__main__":
    App().mainloop()
