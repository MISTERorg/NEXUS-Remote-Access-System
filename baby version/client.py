"""
Optimized Remote Desktop Client
- Deadlock-free socket connection order.
- Frame dropping on overflow to maintain zero input latency.
- Screen dimension scaling and coordinate mapping.
"""

import socket
import struct
import threading
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import io
import queue

class RemoteDesktopClient:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Remote Desktop Client")
        self.video_sock = None
        self.cmd_sock = None
        self.original_width = 0
        self.original_height = 0

        # Bounded frame queue prevents accumulation lag
        self.frame_queue = queue.Queue(maxsize=2)
        self.connected = False

        self.frame = tk.Frame(self.root)
        self.frame.pack(padx=5, pady=5)

        tk.Label(self.frame, text="Server IP:").grid(row=0, column=0, sticky='e')
        self.ip_entry = tk.Entry(self.frame, width=15)
        self.ip_entry.insert(0, "127.0.0.1")
        self.ip_entry.grid(row=0, column=1)

        self.connect_btn = tk.Button(self.frame, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=2, padx=5)

        self.disconnect_btn = tk.Button(self.frame, text="Disconnect", command=self.disconnect, state=tk.DISABLED)
        self.disconnect_btn.grid(row=0, column=3)

        self.canvas = tk.Canvas(self.root, width=800, height=600, bg='black')
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Event Bindings
        self.canvas.bind("<Motion>",          self.on_mouse_move)
        self.canvas.bind("<ButtonPress>",     self.on_mouse_down)
        self.canvas.bind("<ButtonRelease>",   self.on_mouse_up)
        self.canvas.bind("<MouseWheel>",      self.on_mouse_wheel)
        self.canvas.bind("<Button-4>",        self.on_mouse_wheel_up)
        self.canvas.bind("<Button-5>",        self.on_mouse_wheel_down)
        self.canvas.bind("<KeyPress>",        self.on_key_press)
        self.canvas.bind("<Button-1>",        self.focus_canvas)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def connect(self):
        server_ip = self.ip_entry.get().strip()
        if not server_ip:
            messagebox.showerror("Error", "Enter server IP")
            return
        try:
            # 1. Connect Video Socket
            self.video_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.video_sock.settimeout(5.0)
            self.video_sock.connect((server_ip, 12345))

            # 2. Connect Command Socket immediately to complete server accept loop
            self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_sock.settimeout(5.0)
            self.cmd_sock.connect((server_ip, 12346))

            # 3. Read Screen Dimensions Header now that both connections are ready
            header = self.recv_all(8)
            if len(header) < 8:
                raise Exception("Could not read screen resolution. Server didn't send resolution header.")
            self.original_width, self.original_height = struct.unpack('>II', header)

            # 4. Clear connection timeouts for continuous streaming
            self.video_sock.settimeout(None)
            self.cmd_sock.settimeout(None)

            self.connected = True
            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)

            self.video_thread = threading.Thread(target=self.receive_frames, daemon=True)
            self.video_thread.start()

            self.poll_queue()
            self.canvas.focus_set()
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))
            self.cleanup()

    def disconnect(self):
        self.connected = False
        self.cleanup()
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.canvas.delete("all")

    def cleanup(self):
        for sock in (self.video_sock, self.cmd_sock):
            if sock:
                try:
                    sock.close()
                except:
                    pass
        self.video_sock = None
        self.cmd_sock = None

    def recv_all(self, n):
        data = b''
        while len(data) < n:
            try:
                packet = self.video_sock.recv(n - len(data))
                if not packet:
                    return b''
                data += packet
            except:
                return b''
        return data

    def receive_frames(self):
        try:
            while self.connected and self.video_sock:
                length_data = self.recv_all(4)
                if not length_data:
                    break
                frame_len = struct.unpack('>I', length_data)[0]
                jpeg_data = self.recv_all(frame_len)
                if not jpeg_data:
                    break

                image = Image.open(io.BytesIO(jpeg_data))

                # Drop oldest frame if canvas GUI thread is behind
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                self.frame_queue.put(image)
        except Exception as e:
            print("Frame receive error:", e)
        finally:
            self.connected = False

    def poll_queue(self):
        try:
            while True:
                image = self.frame_queue.get_nowait()
                cw = self.canvas.winfo_width()
                ch = self.canvas.winfo_height()
                if cw > 1 and ch > 1:
                    img = image.copy()
                    img.thumbnail((cw, ch), Image.Resampling.BILINEAR)
                    self.photo = ImageTk.PhotoImage(img)

                    x = (cw - img.width) // 2
                    y = (ch - img.height) // 2
                    self.canvas.delete("all")
                    self.canvas.create_image(x, y, anchor=tk.NW, image=self.photo)
                    self.canvas.image = self.photo
        except queue.Empty:
            pass

        if self.connected:
            self.root.after(15, self.poll_queue)
        else:
            self.disconnect()

    # ------------------- Coordinate scaling --------------------
    def scale_coords(self, canvas_x, canvas_y):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 0 or ch <= 0 or self.original_width <= 0:
            return None

        if hasattr(self, 'photo') and self.photo:
            img_w = self.photo.width()
            img_h = self.photo.height()
        else:
            img_w, img_h = cw, ch

        offset_x = (cw - img_w) // 2
        offset_y = (ch - img_h) // 2
        rel_x = canvas_x - offset_x
        rel_y = canvas_y - offset_y

        if rel_x < 0 or rel_y < 0 or rel_x >= img_w or rel_y >= img_h:
            return None

        orig_x = int(rel_x * self.original_width / img_w)
        orig_y = int(rel_y * self.original_height / img_h)
        return orig_x, orig_y

    # ------------------- Command sending ----------------------
    def send_command(self, cmd_str):
        if self.cmd_sock and self.connected:
            try:
                data = cmd_str.encode('utf-8')
                self.cmd_sock.sendall(struct.pack('>H', len(data)) + data)
            except Exception as e:
                print(f"[SEND ERROR] {e}")
                self.disconnect()

    # ------------------- Mouse & Key events ---------------------
    def focus_canvas(self, event):
        self.canvas.focus_set()

    def on_mouse_move(self, event):
        coords = self.scale_coords(event.x, event.y)
        if coords:
            self.send_command(f"MOVE {coords[0]} {coords[1]}")

    def on_mouse_down(self, event):
        coords = self.scale_coords(event.x, event.y)
        if coords:
            button = 'left' if event.num == 1 else ('middle' if event.num == 2 else 'right')
            self.send_command(f"DOWN {coords[0]} {coords[1]} {button}")

    def on_mouse_up(self, event):
        coords = self.scale_coords(event.x, event.y)
        if coords:
            button = 'left' if event.num == 1 else ('middle' if event.num == 2 else 'right')
            self.send_command(f"UP {coords[0]} {coords[1]} {button}")

    def on_mouse_wheel(self, event):
        delta = 1 if event.delta > 0 else -1
        self.send_command(f"SCROLL {delta}")

    def on_mouse_wheel_up(self, event):
        self.send_command("SCROLL 1")

    def on_mouse_wheel_down(self, event):
        self.send_command("SCROLL -1")

    def on_key_press(self, event):
        if not self.connected:
            return
        self.send_command(f"KEY {event.keysym}")
        return "break"

    def on_closing(self):
        self.disconnect()
        self.root.destroy()

if __name__ == '__main__':
    client = RemoteDesktopClient()
    client.root.mainloop()