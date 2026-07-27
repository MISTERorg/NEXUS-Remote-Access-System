"""
Optimized Remote Desktop Server
- Fixed deadlock sequence on startup.
- Added exact-byte TCP receiver for incoming commands.
- Frame-rate throttling to keep bandwidth low and responsive.
"""

import socket
import threading
import struct
import pyautogui
import io
import time

# Disable PyAutoGUI default pause delay and screen corner crash fail-safe
pyautogui.PAUSE = 0.001
pyautogui.FAILSAFE = False

KEY_MAP = {
    'Return':       'enter',
    'BackSpace':    'backspace',
    'Tab':          'tab',
    'Escape':       'esc',
    'space':        'space',
    'Up':           'up',
    'Down':         'down',
    'Left':         'left',
    'Right':        'right',
    'Shift_L':      'shiftleft',
    'Shift_R':      'shiftright',
    'Control_L':    'ctrlleft',
    'Control_R':    'ctrlright',
    'Alt_L':        'altleft',
    'Alt_R':        'altright',
    'Caps_Lock':    'capslock',
    'Num_Lock':     'numlock',
    'Scroll_Lock':  'scrolllock',
    'Home':         'home',
    'End':          'end',
    'Page_Up':      'pageup',
    'Page_Down':    'pagedown',
    'Insert':       'insert',
    'Delete':       'delete',
    'F1': 'f1', 'F2': 'f2', 'F3': 'f3', 'F4': 'f4',
    'F5': 'f5', 'F6': 'f6', 'F7': 'f7', 'F8': 'f8',
    'F9': 'f9', 'F10': 'f10', 'F11': 'f11', 'F12': 'f12',
}

def convert_key(tk_key):
    """Convert Tkinter keysym to PyAutoGUI key string."""
    return KEY_MAP.get(tk_key, tk_key)

def recv_exact(conn, length):
    """Ensure exact number of bytes are read from socket stream."""
    data = b''
    while len(data) < length:
        packet = conn.recv(length - len(data))
        if not packet:
            return None
        data += packet
    return data

def handle_video(conn, addr):
    """Send screen size first, then continuously stream JPEG frames."""
    print(f"[Video Thread] Active for client: {addr}")
    try:
        width, height = pyautogui.size()
        # Send screen dimensions (8 bytes: 2 unsigned integers, big-endian)
        conn.sendall(struct.pack('>II', width, height))

        while True:
            img = pyautogui.screenshot()
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=35)
            data = buf.getvalue()

            # Send frame payload
            conn.sendall(struct.pack('>I', len(data)) + data)

            # ~30 FPS throttle
            time.sleep(0.033)

    except (BrokenPipeError, ConnectionResetError, OSError):
        print(f"[Video Thread] Disconnected: {addr}")
    finally:
        conn.close()

def handle_commands(conn, addr):
    """Receive and execute mouse/keyboard commands."""
    print(f"[Cmd Thread] Active for client: {addr}")
    try:
        while True:
            header = recv_exact(conn, 2)
            if not header:
                break
            length = struct.unpack('>H', header)[0]

            cmd_bytes = recv_exact(conn, length)
            if not cmd_bytes:
                break

            cmd = cmd_bytes.decode('utf-8')
            parts = cmd.split()
            if not parts:
                continue

            action = parts[0]
            if action == 'MOVE':
                x, y = int(parts[1]), int(parts[2])
                pyautogui.moveTo(x, y)
            elif action == 'CLICK':
                x, y, button = int(parts[1]), int(parts[2]), parts[3]
                pyautogui.click(x, y, button=button)
            elif action == 'DOWN':
                x, y, button = int(parts[1]), int(parts[2]), parts[3]
                pyautogui.mouseDown(x, y, button=button)
            elif action == 'UP':
                x, y, button = int(parts[1]), int(parts[2]), parts[3]
                pyautogui.mouseUp(x, y, button=button)
            elif action == 'SCROLL':
                clicks = int(parts[1])
                pyautogui.scroll(clicks * 100)
            elif action == 'KEY':
                key = ' '.join(parts[1:])
                pyautogui.press(convert_key(key))
            elif action == 'KEYDOWN':
                key = ' '.join(parts[1:])
                pyautogui.keyDown(convert_key(key))
            elif action == 'KEYUP':
                key = ' '.join(parts[1:])
                pyautogui.keyUp(convert_key(key))
            elif action == 'TYPE':
                text = ' '.join(parts[1:])
                pyautogui.typewrite(text)

    except Exception as e:
        print(f"[Cmd Thread] Exception: {e}")
    finally:
        conn.close()
        print(f"[Cmd Thread] Disconnected: {addr}")

def main():
    HOST = '0.0.0.0'
    VIDEO_PORT = 12345
    CMD_PORT = 12346

    video_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_sock.bind((HOST, VIDEO_PORT))
    video_sock.listen(1)

    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cmd_sock.bind((HOST, CMD_PORT))
    cmd_sock.listen(1)

    print(f"Server ready. Listening on ports - Video: {VIDEO_PORT}, Cmd: {CMD_PORT}")

    while True:
        try:
            print("\nWaiting for client connection...")
            v_conn, v_addr = video_sock.accept()
            print(f"Video port connected from {v_addr}")

            c_conn, c_addr = cmd_sock.accept()
            print(f"Command port connected from {c_addr}")

            t1 = threading.Thread(target=handle_video, args=(v_conn, v_addr), daemon=True)
            t2 = threading.Thread(target=handle_commands, args=(c_conn, c_addr), daemon=True)
            t1.start()
            t2.start()

            t1.join()
            t2.join()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            break
        except Exception as e:
            print(f"Server socket error: {e}")

    video_sock.close()
    cmd_sock.close()

if __name__ == '__main__':
    main()