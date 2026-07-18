# ============================================================================
# Minecraft Autoclicker by Rane
# A simple autoclicker utility for Minecraft with configurable hotkeys,
# separate left/right click intervals, spam click and hold modes,
# system tray support, and audio feedback.
#
# Click engine uses Win32 SendInput() via ctypes so events are injected at
# the hardware-driver level. This is required for Minecraft gameplay because
# the game uses WM_INPUT (raw input) during play, which completely bypasses
# the Windows message queue that pynput/SendMessage writes to. SendInput()
# reaches WM_INPUT and is therefore visible to Minecraft during gameplay.
#
# Single instance enforcement via Windows mutex.
# Emergency force quit: Esc + Backspace (shown in UI note).
# ============================================================================

# --- Standard Library Imports ---
import sys          # Application control flow and exit
import time         # Sleep intervals for click timing
import configparser # INI config file read/write
import os           # File and path operations
import winsound     # WAV audio playback (Windows built-in, no extra deps)
import ctypes       # Win32 API access for SendInput()
import ctypes.wintypes as wintypes  # Win32 type aliases (DWORD, LONG, etc.)

# --- Third-Party Imports ---
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QSpinBox,
    QRadioButton, QButtonGroup, QComboBox,
    QCheckBox, QSystemTrayIcon, QMenu, QMessageBox
)
from PyQt6.QtGui import QFont, QIcon, QAction
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent

import pynput.keyboard as pynput_keyboard  # Global hotkey listener (keyboard only)


# ============================================================================
# Win32 SendInput Structures
# ============================================================================
# These mirror the MOUSEINPUT / INPUT structs from winuser.h exactly.
# SendInput() requires the structs to be laid out in memory identically to
# the C definitions so ctypes can pass them directly to the Win32 API.

# dwFlags values for MOUSEINPUT
MOUSEEVENTF_LEFTDOWN:   int = 0x0002  # Left button press
MOUSEEVENTF_LEFTUP:     int = 0x0004  # Left button release
MOUSEEVENTF_RIGHTDOWN:  int = 0x0008  # Right button press
MOUSEEVENTF_RIGHTUP:    int = 0x0010  # Right button release

# INPUT type discriminator
INPUT_MOUSE: int = 0


class MOUSEINPUT(ctypes.Structure):
    """
    Mirror of the Win32 MOUSEINPUT struct (winuser.h).

    dx, dy are ignored when not using MOUSEEVENTF_MOVE.
    mouseData, dwExtraInfo are 0 for standard clicks.
    time=0 tells the system to fill in the timestamp automatically.
    """
    _fields_ = [
        ("dx",          wintypes.LONG),
        ("dy",          wintypes.LONG),
        ("mouseData",   wintypes.DWORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT_UNION(ctypes.Union):
    """Union field inside INPUT — only the mi (mouse input) member is used here."""
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    """
    Mirror of the Win32 INPUT struct (winuser.h).

    type=INPUT_MOUSE selects the mi union member.
    """
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


def _send_mouse_input(flags: int) -> None:
    """
    Fire a single mouse event via Win32 SendInput().

    SendInput() injects events at the hardware-abstraction level, which means
    they pass through the raw-input path (WM_INPUT) and are visible to
    applications like Minecraft that capture raw mouse events during gameplay.
    pynput uses SendMessage/PostMessage which only reaches the message queue
    and is invisible to WM_INPUT consumers.

    Args:
        flags: One of the MOUSEEVENTF_* constants defined above.
    """
    event = INPUT(
        type=INPUT_MOUSE,
        _input=_INPUT_UNION(
            mi=MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=None,
            )
        ),
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


# ============================================================================
# Constants
# ============================================================================

# Config stored in %LOCALAPPDATA% — user-scoped, survives script relocation.
CONFIG_PATH: str = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Minecraft_Autoclicker_Settings.ini"
)

# Asset paths resolved relative to the script file so they work from any
# working directory, including when launched at Windows startup.
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
ICON_PATH:   str = os.path.join(_SCRIPT_DIR, "click.ico")
SOUND_START: str = os.path.join(_SCRIPT_DIR, "start.wav")
SOUND_STOP:  str = os.path.join(_SCRIPT_DIR, "stop.wav")

# Default config values
DEFAULT_HOTKEY:             str  = "f8"
DEFAULT_LEFT_INTERVAL:      int  = 100    # milliseconds
DEFAULT_RIGHT_INTERVAL:     int  = 100    # milliseconds
DEFAULT_MOUSE_BUTTON:       str  = "left"
DEFAULT_CLICK_MODE:         str  = "spam"
DEFAULT_MINIMIZE_TO_TRAY:   bool = False

# UI sizing
WINDOW_MIN_WIDTH:  int = 440
SPINBOX_MIN_WIDTH: int = 80

# Click mode identifiers
MODE_SPAM: str = "spam"
MODE_HOLD: str = "hold"

# Button identifiers
BTN_LEFT:  str = "left"
BTN_RIGHT: str = "right"
BTN_BOTH:  str = "both"          # <--- NEW

# Single instance mutex name
MUTEX_NAME: str = "MinecraftAutoclicker_Rane_SingleInstance"


# ============================================================================
# Single Instance Enforcement
# ============================================================================

def ensure_single_instance() -> bool:
    """
    Create a named mutex using Win32 API.
    Returns True if this is the first instance, False if another instance is running.
    """
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    # Keep mutex handle alive by storing it in a global (prevents garbage collection)
    global _mutex_handle
    _mutex_handle = mutex
    return True


# ============================================================================
# Config Manager
# ============================================================================

class ConfigManager:
    """
    Handles reading and writing of the INI configuration file.

    Stores settings in %LOCALAPPDATA%\\Minecraft_Autoclicker_Settings.ini
    so the config is user-scoped and independent of the script's location.

    Persists: hotkey, left/right intervals, selected mouse button,
    selected click mode, and minimize-to-tray.
    """

    def __init__(self, config_path: str) -> None:
        """
        Initialise the config manager and ensure the config file exists.

        Args:
            config_path: Full path to the INI config file.
        """
        self.config_path = config_path
        self.config      = configparser.ConfigParser()
        self._ensure_config_exists()
        self.config.read(self.config_path)

    def _ensure_config_exists(self) -> None:
        """Create the config file with sensible defaults if it is missing."""
        if not os.path.exists(self.config_path):
            self.config["Hotkey"] = {
                "toggle_key": DEFAULT_HOTKEY,
            }
            self.config["Intervals"] = {
                "left_interval_ms":  str(DEFAULT_LEFT_INTERVAL),
                "right_interval_ms": str(DEFAULT_RIGHT_INTERVAL),
            }
            self.config["Selection"] = {
                "mouse_button": DEFAULT_MOUSE_BUTTON,
                "click_mode":   DEFAULT_CLICK_MODE,
            }
            self.config["Options"] = {
                "minimize_to_tray": str(DEFAULT_MINIMIZE_TO_TRAY),
            }
            with open(self.config_path, "w") as config_file:
                self.config.write(config_file)
            print(f"[CONFIG] Created default config at: {self.config_path}")

    # --- Getters ---

    def get_hotkey(self) -> str:
        """Return the configured toggle hotkey string."""
        return self.config.get("Hotkey", "toggle_key", fallback=DEFAULT_HOTKEY)

    def get_left_interval(self) -> int:
        """Return the configured left-click interval in milliseconds."""
        return self.config.getint("Intervals", "left_interval_ms", fallback=DEFAULT_LEFT_INTERVAL)

    def get_right_interval(self) -> int:
        """Return the configured right-click interval in milliseconds."""
        return self.config.getint("Intervals", "right_interval_ms", fallback=DEFAULT_RIGHT_INTERVAL)

    def get_mouse_button(self) -> str:
        """Return the last selected mouse button ('left', 'right', or 'both')."""
        return self.config.get("Selection", "mouse_button", fallback=DEFAULT_MOUSE_BUTTON)

    def get_click_mode(self) -> str:
        """Return the last selected click mode ('spam' or 'hold')."""
        return self.config.get("Selection", "click_mode", fallback=DEFAULT_CLICK_MODE)

    def get_minimize_to_tray(self) -> bool:
        """Return whether the app should minimize to tray instead of taskbar."""
        return self.config.getboolean("Options", "minimize_to_tray", fallback=DEFAULT_MINIMIZE_TO_TRAY)

    # --- Unified save ---

    def save(
        self,
        hotkey:             str,
        left_interval:      int,
        right_interval:     int,
        mouse_button:       str,
        click_mode:         str,
        minimize_to_tray:   bool,
    ) -> None:
        """
        Persist all settings to the INI file in one atomic write.

        Args:
            hotkey:             Hotkey string (e.g. 'f8').
            left_interval:      Left-click interval in milliseconds.
            right_interval:     Right-click interval in milliseconds.
            mouse_button:       Selected button ('left', 'right', or 'both').
            click_mode:         Selected mode ('spam' or 'hold').
            minimize_to_tray:   Whether to minimize to system tray.
        """
        self.config["Hotkey"] = {
            "toggle_key": hotkey,
        }
        self.config["Intervals"] = {
            "left_interval_ms":  str(left_interval),
            "right_interval_ms": str(right_interval),
        }
        self.config["Selection"] = {
            "mouse_button": mouse_button,
            "click_mode":   click_mode,
        }
        self.config["Options"] = {
            "minimize_to_tray": str(minimize_to_tray),
        }
        with open(self.config_path, "w") as config_file:
            self.config.write(config_file)
        print(
            f"[CONFIG] Saved — hotkey={hotkey}, left={left_interval}ms, "
            f"right={right_interval}ms, button={mouse_button}, "
            f"mode={click_mode}, tray={minimize_to_tray}"
        )


# ============================================================================
# Click Worker Thread
# ============================================================================

class ClickWorker(QThread):
    """
    Background thread that performs mouse clicking via Win32 SendInput().

    WHY SendInput() INSTEAD OF PYNPUT:
    Minecraft uses WM_INPUT (raw input API) during gameplay to read mouse
    events directly from the hardware input stream. pynput routes through
    SendMessage/PostMessage which only delivers to the standard Windows
    message queue — WM_INPUT consumers never see those events. SendInput()
    injects at the hardware-abstraction (KMDF) level, which feeds the raw
    input path, making clicks visible to Minecraft in all contexts.

    In SPAM mode:
      - For 'left' or 'right': fires BUTTONDOWN + BUTTONUP pairs at the
        configured interval for that button.
      - For 'both': fires left clicks at the left interval and right clicks
        at the right interval, independently and interleaved.
    In HOLD mode: sends BUTTONDOWN on start, BUTTONUP on stop (both buttons
    together for 'both').

    Signals:
        status_signal: Emits a human-readable status string for the UI label.
    """

    status_signal = pyqtSignal(str)

    def __init__(self, button: str, mode: str, left_interval_ms: int, right_interval_ms: int = None) -> None:
        """
        Initialise the click worker.

        Args:
            button:           Which mouse button to use ('left', 'right', or 'both').
            mode:             Click mode ('spam' or 'hold').
            left_interval_ms: Left-click interval in milliseconds.
            right_interval_ms: Right-click interval in milliseconds (only used for 'both').
        """
        super().__init__()
        self.button      = button
        self.mode        = mode
        self.left_ms     = left_interval_ms
        self.right_ms    = right_interval_ms if right_interval_ms is not None else left_interval_ms
        self._running    = False

        # Resolve the SendInput flag(s) for this button.
        if button == BTN_LEFT:
            self._flags_down = [MOUSEEVENTF_LEFTDOWN]
            self._flags_up   = [MOUSEEVENTF_LEFTUP]
        elif button == BTN_RIGHT:
            self._flags_down = [MOUSEEVENTF_RIGHTDOWN]
            self._flags_up   = [MOUSEEVENTF_RIGHTUP]
        else:  # both
            self._flags_down = [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN]
            self._flags_up   = [MOUSEEVENTF_LEFTUP,   MOUSEEVENTF_RIGHTUP]

        print(f"[WORKER] Initialised — button={button}, mode={mode}, "
              f"left={self.left_ms}ms, right={self.right_ms}ms")

    def run(self) -> None:
        """Execute the click loop on the worker thread."""
        self._running = True
        self.status_signal.emit(
            f"● Active — {self.button.capitalize()} / {self.mode.capitalize()}"
        )
        print(f"[WORKER] Click loop started — {self.button} {self.mode}")

        if self.mode == MODE_HOLD:
            self._run_hold()
        else:
            self._run_spam()

    def _run_spam(self) -> None:
        """
        Spam-click loop.

        For single buttons: sends BUTTONDOWN + BUTTONUP pairs at that button's interval.
        For 'both': uses two independent timers – left clicks at left_ms,
        right clicks at right_ms – interleaved in the same loop.
        """
        if self.button == BTN_BOTH:
            self._run_spam_both()
        else:
            self._run_spam_single()

    def _run_spam_single(self) -> None:
        """Spam a single mouse button at its own interval."""
        interval_sec = self.left_ms / 1000.0  # (left_ms is the only one used for single buttons)
        hold_sec = 0.025
        while self._running:
            for flag in self._flags_down:
                _send_mouse_input(flag)
            time.sleep(hold_sec)
            for flag in self._flags_up:
                _send_mouse_input(flag)
            time.sleep(interval_sec)

    def _run_spam_both(self) -> None:
        """
        Spam both buttons independently using two timers.

        Left clicks fire every left_ms, right clicks every right_ms.
        They are interleaved according to whichever timer expires first.
        """
        left_sec = self.left_ms / 1000.0
        right_sec = self.right_ms / 1000.0

        # Time of the last click for each button (start at 0 so both fire immediately)
        last_left = 0.0
        last_right = 0.0

        while self._running:
            now = time.monotonic()
            left_due = (now - last_left) >= left_sec
            right_due = (now - last_right) >= right_sec

            if left_due:
                _send_mouse_input(MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.025)
                _send_mouse_input(MOUSEEVENTF_LEFTUP)
                last_left = now

            if right_due:
                _send_mouse_input(MOUSEEVENTF_RIGHTDOWN)
                time.sleep(0.025)
                _send_mouse_input(MOUSEEVENTF_RIGHTUP)
                last_right = now

            # If nothing was due, sleep a tiny bit to avoid busy-wait
            if not left_due and not right_due:
                time.sleep(0.001)

    def _run_hold(self) -> None:
        """
        Hold the mouse button(s) by sending BUTTONDOWN and idling until stopped,
        then sending BUTTONUP to release.
        For 'both', we send both DOWN flags together.
        """
        for flag in self._flags_down:
            _send_mouse_input(flag)
        while self._running:
            time.sleep(0.05)  # Low-CPU idle — thread stays alive until stop()
        for flag in self._flags_up:
            _send_mouse_input(flag)
        print(f"[WORKER] Released held button(s): {self.button}")

    def stop(self) -> None:
        """Signal the click loop to exit on its next iteration."""
        self._running = False
        print(f"[WORKER] Stop requested — {self.button} {self.mode}")


# ============================================================================
# Hotkey Listener Thread
# ============================================================================

class HotkeyListener(QThread):
    """
    Runs a pynput global keyboard listener in a background thread.

    Listens system-wide so the hotkey fires even when Minecraft has focus.
    Only the keyboard listener uses pynput — mouse events use SendInput().

    Signals:
        toggled_signal:   Emitted each time the configured toggle key is pressed.
        force_quit_signal: Emitted when Esc + Backspace are pressed together.
    """

    toggled_signal = pyqtSignal()
    force_quit_signal = pyqtSignal()

    def __init__(self, hotkey: str) -> None:
        """
        Initialise the hotkey listener.

        Args:
            hotkey: The pynput key name to listen for (e.g. 'f8').
        """
        super().__init__()
        self.hotkey    = hotkey
        self._listener = None
        self.esc_down = False
        self.backspace_down = False
        print(f"[HOTKEY] Listener initialised for key: {hotkey}")

    def run(self) -> None:
        """Start the blocking pynput keyboard listener with press and release callbacks."""

        def on_press(key: object) -> None:
            """Update key states and emit signals when conditions are met."""
            # Force quit combination: Esc + Backspace
            try:
                if key == pynput_keyboard.Key.esc:
                    self.esc_down = True
                elif key == pynput_keyboard.Key.backspace:
                    self.backspace_down = True
            except AttributeError:
                pass

            # Check if both are pressed
            if self.esc_down and self.backspace_down:
                print("[HOTKEY] Force quit combination (Esc+Backspace) detected")
                self.force_quit_signal.emit()

            # Normal toggle hotkey (only on press, not on release)
            try:
                key_name = key.name   # Special keys expose .name (F1–F12 etc.)
            except AttributeError:
                key_name = key.char   # Regular character keys expose .char

            if key_name and key_name.lower() == self.hotkey.lower():
                print(f"[HOTKEY] Toggle key pressed: {key_name}")
                self.toggled_signal.emit()

        def on_release(key: object) -> None:
            """Reset key states when keys are released."""
            try:
                if key == pynput_keyboard.Key.esc:
                    self.esc_down = False
                elif key == pynput_keyboard.Key.backspace:
                    self.backspace_down = False
            except AttributeError:
                pass

        with pynput_keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            self._listener = listener
            listener.join()

    def stop(self) -> None:
        """Stop the keyboard listener cleanly."""
        if self._listener:
            self._listener.stop()
        print("[HOTKEY] Listener stopped")


# ============================================================================
# Main Application Window
# ============================================================================

class MinecraftAutoclickerApp(QMainWindow):
    """
    Main application window for the Minecraft Autoclicker.

    Manages the UI controls, ClickWorker and HotkeyListener threads,
    system tray integration, and WAV audio feedback on toggle events.
    """

    def __init__(self) -> None:
        """Initialise the application window and all subsystems."""
        super().__init__()
        print("[INIT] Initialising MinecraftAutoclickerApp...")

        self._worker:          ClickWorker | None    = None
        self._hotkey_listener: HotkeyListener | None = None
        self._is_active:       bool                  = False
        self._tray_icon:       QSystemTrayIcon | None = None

        self._config = ConfigManager(CONFIG_PATH)

        self.setWindowTitle("Minecraft Autoclicker by Rane")

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
            print(f"[ICON] Loaded icon: {ICON_PATH}")
        else:
            print(f"[ICON] click.ico not found at {ICON_PATH} — skipping")

        self._init_ui()
        self._init_tray()
        self._start_hotkey_listener()

        print("[INIT] MinecraftAutoclickerApp ready")

    # ------------------------------------------------------------------
    # UI Initialisation
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """Set up all UI elements and layouts."""
        print("[UI] Building interface...")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        root_layout = QVBoxLayout(central_widget)
        root_layout.setSpacing(10)
        root_layout.setContentsMargins(16, 16, 16, 16)

        self._build_button_group(root_layout)
        self._build_mode_group(root_layout)
        self._build_interval_group(root_layout)
        self._build_hotkey_group(root_layout)
        self._build_options_group(root_layout)
        self._build_status_bar(root_layout)
        self._build_toggle_button(root_layout)
        self._build_force_quit_note(root_layout)

        self.setMinimumWidth(WINDOW_MIN_WIDTH)
        self.setFixedSize(self.sizeHint())
        self._center_window()

        print("[UI] Interface built successfully")

    def _build_button_group(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the mouse button selection group (Left / Right / Both).

        Restores the last saved selection from config on startup.

        Args:
            parent_layout: Layout to attach this group to.
        """
        group  = QGroupBox("Mouse Button")
        layout = QHBoxLayout(group)

        self._radio_left  = QRadioButton("Left Click")
        self._radio_right = QRadioButton("Right Click")
        self._radio_both  = QRadioButton("Both")          # <--- NEW

        # Restore saved selection; default to left if config value is unrecognised
        saved_button = self._config.get_mouse_button()
        if saved_button == BTN_RIGHT:
            self._radio_right.setChecked(True)
        elif saved_button == BTN_BOTH:
            self._radio_both.setChecked(True)
        else:
            self._radio_left.setChecked(True)

        self._button_group = QButtonGroup()
        self._button_group.addButton(self._radio_left,  0)
        self._button_group.addButton(self._radio_right, 1)
        self._button_group.addButton(self._radio_both,  2)   # <--- NEW

        # Save whenever the selection changes
        self._radio_left.toggled.connect(self._on_setting_changed)
        self._radio_right.toggled.connect(self._on_setting_changed)
        self._radio_both.toggled.connect(self._on_setting_changed)   # <--- NEW

        layout.addWidget(self._radio_left)
        layout.addWidget(self._radio_right)
        layout.addWidget(self._radio_both)   # <--- NEW
        parent_layout.addWidget(group)

    def _build_mode_group(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the click mode selection group (Spam / Hold).

        Restores the last saved selection from config on startup.

        Args:
            parent_layout: Layout to attach this group to.
        """
        group  = QGroupBox("Click Mode")
        layout = QHBoxLayout(group)

        self._radio_spam = QRadioButton("Spam Click")
        self._radio_hold = QRadioButton("Hold Button")

        # Restore saved selection; default to spam if config value is unrecognised
        saved_mode = self._config.get_click_mode()
        if saved_mode == MODE_HOLD:
            self._radio_hold.setChecked(True)
        else:
            self._radio_spam.setChecked(True)

        self._mode_group = QButtonGroup()
        self._mode_group.addButton(self._radio_spam, 0)
        self._mode_group.addButton(self._radio_hold, 1)

        # Save whenever the selection changes
        self._radio_spam.toggled.connect(self._on_setting_changed)
        self._radio_hold.toggled.connect(self._on_setting_changed)

        layout.addWidget(self._radio_spam)
        layout.addWidget(self._radio_hold)
        parent_layout.addWidget(group)

    def _build_interval_group(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the interval input group for left and right click delays.

        Args:
            parent_layout: Layout to attach this group to.
        """
        group  = QGroupBox("Click Intervals")
        layout = QHBoxLayout(group)

        left_label      = QLabel("Left (ms):")
        self._spin_left = QSpinBox()
        self._spin_left.setRange(1, 10000)
        self._spin_left.setValue(self._config.get_left_interval())
        self._spin_left.setMinimumWidth(SPINBOX_MIN_WIDTH)
        self._spin_left.valueChanged.connect(self._on_setting_changed)

        right_label      = QLabel("Right (ms):")
        self._spin_right = QSpinBox()
        self._spin_right.setRange(1, 10000)
        self._spin_right.setValue(self._config.get_right_interval())
        self._spin_right.setMinimumWidth(SPINBOX_MIN_WIDTH)
        self._spin_right.valueChanged.connect(self._on_setting_changed)

        layout.addWidget(left_label)
        layout.addWidget(self._spin_left)
        layout.addSpacing(16)
        layout.addWidget(right_label)
        layout.addWidget(self._spin_right)
        parent_layout.addWidget(group)

    def _build_hotkey_group(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the hotkey selection group.

        Args:
            parent_layout: Layout to attach this group to.
        """
        group  = QGroupBox("Toggle Hotkey")
        layout = QHBoxLayout(group)

        hotkey_label       = QLabel("Key:")
        self._hotkey_combo = QComboBox()

        common_keys = [
            "f1", "f2", "f3", "f4", "f5", "f6",
            "f7", "f8", "f9", "f10", "f11", "f12",
            "insert", "delete", "home", "end",
            "page_up", "page_down",
            "caps_lock", "scroll_lock", "num_lock",
        ]
        self._hotkey_combo.addItems(common_keys)

        saved_key = self._config.get_hotkey().lower()
        self._hotkey_combo.setCurrentText(
            saved_key if saved_key in common_keys else DEFAULT_HOTKEY
        )
        self._hotkey_combo.currentTextChanged.connect(self._on_hotkey_changed)

        layout.addWidget(hotkey_label)
        layout.addWidget(self._hotkey_combo)
        parent_layout.addWidget(group)

    def _build_options_group(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the options group with minimize-to-tray checkbox.

        Args:
            parent_layout: Layout to attach this group to.
        """
        group  = QGroupBox("Options")
        layout = QVBoxLayout(group)

        self._chk_tray = QCheckBox("Minimize to system tray")
        self._chk_tray.setChecked(self._config.get_minimize_to_tray())
        self._chk_tray.stateChanged.connect(self._on_setting_changed)

        layout.addWidget(self._chk_tray)
        parent_layout.addWidget(group)

    def _build_status_bar(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the status indicator label shown above the toggle button.

        Args:
            parent_layout: Layout to attach this to.
        """
        self._status_label = QLabel("● Idle")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._status_label.font()
        font.setBold(True)
        self._status_label.setFont(font)
        parent_layout.addWidget(self._status_label)

    def _build_toggle_button(self, parent_layout: QVBoxLayout) -> None:
        """
        Build the main Start / Stop toggle button.

        Args:
            parent_layout: Layout to attach this to.
        """
        self._toggle_button = QPushButton("Start ▶")
        self._toggle_button.setMinimumHeight(36)
        self._toggle_button.clicked.connect(self._on_toggle)
        parent_layout.addWidget(self._toggle_button)

    def _build_force_quit_note(self, parent_layout: QVBoxLayout) -> None:
        """
        Build a small note label indicating the emergency force quit hotkey.

        Args:
            parent_layout: Layout to attach this to.
        """
        note_label = QLabel("Emergency stop: Press Esc + Backspace together to force quit the autoclicker.")
        note_label.setWordWrap(True)
        note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = note_label.font()
        font.setItalic(True)
        font.setPointSize(8)
        note_label.setFont(font)
        note_label.setStyleSheet("color: gray;")
        parent_layout.addWidget(note_label)

    def _center_window(self) -> None:
        """Center the window on the primary screen."""
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width()  - self.width())  // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)
        print("[WINDOW] Centered on screen")

    # ------------------------------------------------------------------
    # System Tray
    # ------------------------------------------------------------------

    def _init_tray(self) -> None:
        """Set up the system tray icon and its right-click context menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[TRAY] System tray not available on this system")
            return

        icon = QIcon(ICON_PATH) if os.path.exists(ICON_PATH) else QIcon()
        self._tray_icon = QSystemTrayIcon(icon, parent=self)
        self._tray_icon.setToolTip("Minecraft Autoclicker by Rane")

        tray_menu   = QMenu()
        action_show = QAction("Show", self)
        action_quit = QAction("Quit", self)
        action_show.triggered.connect(self._restore_from_tray)
        action_quit.triggered.connect(self._quit_app)
        tray_menu.addAction(action_show)
        tray_menu.addSeparator()
        tray_menu.addAction(action_quit)

        self._tray_icon.setContextMenu(tray_menu)
        # Double-clicking the tray icon restores the main window
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()
        print("[TRAY] System tray icon initialised")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """
        Restore the window when the tray icon is double-clicked.

        Args:
            reason: The activation reason from the tray icon signal.
        """
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        """Show and raise the main window from the system tray."""
        self.showNormal()
        self.activateWindow()
        print("[TRAY] Window restored from tray")

    def _quit_app(self) -> None:
        """Stop all threads and exit the application via the tray menu."""
        print("[APP] Quit requested from tray menu")
        self._cleanup_threads()
        QApplication.quit()

    # ------------------------------------------------------------------
    # Audio Feedback
    # ------------------------------------------------------------------

    def _play_sound(self, sound_path: str) -> None:
        """
        Play a WAV file asynchronously using the built-in winsound module.

        SND_ASYNC means playback never blocks the UI thread or click worker.
        Silently skips if the file does not exist.

        Args:
            sound_path: Absolute path to the .wav file to play.
        """
        if not os.path.exists(sound_path):
            print(f"[SOUND] File not found, skipping: {sound_path}")
            return
        try:
            winsound.PlaySound(
                sound_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC
            )
            print(f"[SOUND] Playing: {os.path.basename(sound_path)}")
        except Exception as sound_error:
            print(f"[SOUND] Playback failed: {sound_error}")

    # ------------------------------------------------------------------
    # Hotkey Listener Management
    # ------------------------------------------------------------------

    def _start_hotkey_listener(self) -> None:
        """(Re)start the background hotkey listener with the current key."""
        if self._hotkey_listener and self._hotkey_listener.isRunning():
            self._hotkey_listener.stop()
            self._hotkey_listener.wait()

        hotkey = self._hotkey_combo.currentText()
        self._hotkey_listener = HotkeyListener(hotkey)
        self._hotkey_listener.toggled_signal.connect(self._on_toggle)
        self._hotkey_listener.force_quit_signal.connect(self._force_quit)
        self._hotkey_listener.start()
        print(f"[HOTKEY] Listener started for: {hotkey}")

    # ------------------------------------------------------------------
    # Slot Handlers
    # ------------------------------------------------------------------

    def _on_toggle(self) -> None:
        """Toggle the autoclicker on or off."""
        if self._is_active:
            self._stop_clicking()
        else:
            self._start_clicking()

    def _start_clicking(self) -> None:
        """Read current UI settings, play start sound, and launch ClickWorker."""
        # Determine selected button
        if self._radio_left.isChecked():
            button = BTN_LEFT
        elif self._radio_right.isChecked():
            button = BTN_RIGHT
        else:
            button = BTN_BOTH   # <--- NEW

        mode = MODE_SPAM if self._radio_spam.isChecked() else MODE_HOLD

        # Pass the appropriate interval(s) to the worker
        if button == BTN_BOTH:
            worker = ClickWorker(button, mode,
                                 self._spin_left.value(),
                                 self._spin_right.value())
        elif button == BTN_LEFT:
            worker = ClickWorker(button, mode,
                                 self._spin_left.value(), None)
        else:  # right
            worker = ClickWorker(button, mode,
                                 self._spin_right.value(), None)

        print(f"[APP] Starting — button={button}, mode={mode}, "
              f"left={self._spin_left.value()}ms, right={self._spin_right.value()}ms")

        self._play_sound(SOUND_START)

        self._worker = worker
        self._worker.status_signal.connect(self._status_label.setText)
        self._worker.start()

        self._is_active = True
        self._toggle_button.setText("Stop ■")
        self._set_controls_enabled(False)

    def _stop_clicking(self) -> None:
        """Stop the active ClickWorker and play the stop sound."""
        if self._worker:
            self._worker.stop()
            self._worker.wait()
            self._worker = None
            print("[APP] Click worker stopped")

        self._play_sound(SOUND_STOP)

        self._is_active = False
        self._toggle_button.setText("Start ▶")
        self._status_label.setText("● Idle")
        self._set_controls_enabled(True)

    def _force_quit(self) -> None:
        """
        Emergency force quit: stop all threads and exit immediately.
        Called when Esc+Backspace is pressed.
        """
        print("[FORCE QUIT] Esc+Backspace pressed - forcing application quit")
        self._cleanup_threads()
        QApplication.quit()

    def _on_setting_changed(self) -> None:
        """Persist all current settings whenever any control value changes."""
        # Determine selected button
        if self._radio_left.isChecked():
            btn = BTN_LEFT
        elif self._radio_right.isChecked():
            btn = BTN_RIGHT
        else:
            btn = BTN_BOTH   # <--- NEW

        self._config.save(
            hotkey             = self._hotkey_combo.currentText(),
            left_interval      = self._spin_left.value(),
            right_interval     = self._spin_right.value(),
            mouse_button       = btn,
            click_mode         = MODE_SPAM if self._radio_spam.isChecked() else MODE_HOLD,
            minimize_to_tray   = self._chk_tray.isChecked(),
        )

    def _on_hotkey_changed(self, new_key: str) -> None:
        """
        Restart the hotkey listener and save config when the key changes.

        Args:
            new_key: The newly selected hotkey string.
        """
        print(f"[APP] Hotkey changed to: {new_key}")
        self._on_setting_changed()
        self._start_hotkey_listener()

    # ------------------------------------------------------------------
    # Helper Methods
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        """
        Enable or disable all setting controls while the clicker is active.

        Prevents accidentally changing button/mode/interval mid-session.

        Args:
            enabled: True to unlock controls, False to lock them.
        """
        self._radio_left.setEnabled(enabled)
        self._radio_right.setEnabled(enabled)
        self._radio_both.setEnabled(enabled)   # <--- NEW
        self._radio_spam.setEnabled(enabled)
        self._radio_hold.setEnabled(enabled)
        self._spin_left.setEnabled(enabled)
        self._spin_right.setEnabled(enabled)
        self._hotkey_combo.setEnabled(enabled)

    def _cleanup_threads(self) -> None:
        """Stop the click worker and hotkey listener threads cleanly."""
        if self._is_active:
            self._stop_clicking()
        if self._hotkey_listener and self._hotkey_listener.isRunning():
            self._hotkey_listener.stop()
            self._hotkey_listener.wait()
        print("[APP] Threads cleaned up")

    # ------------------------------------------------------------------
    # Qt Event Overrides
    # ------------------------------------------------------------------

    def changeEvent(self, event: object) -> None:
        """
        Intercept minimize events to optionally hide to tray.

        Only hides if the 'Minimize to system tray' checkbox is enabled
        and the tray icon was successfully created.

        Args:
            event: The Qt change event.
        """
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._chk_tray.isChecked()
            and self._tray_icon is not None
        ):
            self.hide()     # Remove from taskbar
            event.ignore()  # Don't let Qt process the minimize further
            print("[TRAY] Window hidden to system tray")
            return
        super().changeEvent(event)

    def closeEvent(self, event: object) -> None:
        """Clean up all threads and hide the tray icon before closing."""
        print("[APP] Close requested — stopping threads...")
        self._cleanup_threads()
        if self._tray_icon:
            self._tray_icon.hide()
        event.accept()
        print("[APP] Application closed")


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    # Enforce single instance
    if not ensure_single_instance():
        # Another instance is already running – show popup and exit
        app = QApplication(sys.argv)  # Needed for QMessageBox
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("Already Running")
        msg.setText("Minecraft Autoclicker is already running.")
        msg.setInformativeText("Only one instance can run at a time.")
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
        sys.exit(1)

    print("[APP] Starting Minecraft Autoclicker...")
    app = QApplication(sys.argv)

    comic_sans = QFont("Comic Sans MS", 9)
    app.setFont(comic_sans)
    print("[APP] Font set to Comic Sans MS")

    window = MinecraftAutoclickerApp()
    window.show()
    print("[APP] Main window displayed")

    sys.exit(app.exec())
