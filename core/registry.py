"""
core/registry.py
----------------
Device registry — tracks all registered agents (computers, servers, IoT, mobile).
Stores device metadata, capabilities, online status, and last-seen timestamps.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from utils.logger import get_logger

log = get_logger("nexus.registry")


class DeviceType(str, Enum):
    DESKTOP = "desktop"
    SERVER = "server"
    IOT = "iot"
    MOBILE = "mobile"
    UNKNOWN = "unknown"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"           # In an active session
    MAINTENANCE = "maintenance"


class DeviceCapabilities(BaseModel):
    screen_share: bool = False
    remote_input: bool = False
    file_transfer: bool = False
    terminal: bool = False
    clipboard: bool = False
    audio: bool = False
    camera: bool = False
    metrics: bool = True    # CPU/RAM/disk stats


class DeviceInfo(BaseModel):
    device_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    device_type: DeviceType = DeviceType.UNKNOWN
    os: Optional[str] = None
    os_version: Optional[str] = None
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)
    tags: List[str] = Field(default_factory=list)
    owner_user_id: Optional[str] = None
    status: DeviceStatus = DeviceStatus.OFFLINE
    registered_at: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.last_seen = time.time()

    def is_stale(self, timeout_s: int = 90) -> bool:
        return (time.time() - self.last_seen) > timeout_s


class DeviceMetrics(BaseModel):
    """Real-time metrics pushed by the agent."""
    device_id: str
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    disk_percent: float = 0.0
    network_rx_bps: float = 0.0
    network_tx_bps: float = 0.0
    uptime_seconds: float = 0.0
    timestamp: float = Field(default_factory=time.time)


class DeviceRegistry:
    def __init__(self):
        self._devices: Dict[str, DeviceInfo] = {}
        self._metrics: Dict[str, DeviceMetrics] = {}
        self._listeners: List[Any] = []
        self._lock = asyncio.Lock()

    async def register(self, device: DeviceInfo) -> DeviceInfo:
        async with self._lock:
            existing = self._devices.get(device.device_id)
            if existing:
                device.registered_at = existing.registered_at
                log.info("registry.device_updated", device_id=device.device_id)
            else:
                log.info("registry.device_registered",
                         device_id=device.device_id,
                         name=device.name,
                         type=device.device_type)
            self._devices[device.device_id] = device
            await self._notify("registered", device)
            return device

    async def get(self, device_id: str) -> Optional[DeviceInfo]:
        return self._devices.get(device_id)

    async def remove(self, device_id: str) -> bool:
        async with self._lock:
            device = self._devices.pop(device_id, None)
            if device:
                await self._notify("removed", device)
                return True
            return False

    async def set_status(self, device_id: str, status: DeviceStatus) -> None:
        device = self._devices.get(device_id)
        if device:
            device.status = status
            device.touch()
            await self._notify("status_changed", device)
            log.debug("registry.status_change",
                      device_id=device_id, status=status)

    async def heartbeat(self, device_id: str) -> None:
        device = self._devices.get(device_id)
        if device:
            device.touch()
            if device.status == DeviceStatus.OFFLINE:
                await self.set_status(device_id, DeviceStatus.ONLINE)

    async def update_metrics(self, metrics: DeviceMetrics) -> None:
        self._metrics[metrics.device_id] = metrics

    async def get_metrics(self, device_id: str) -> Optional[DeviceMetrics]:
        return self._metrics.get(device_id)

    async def list_all(self) -> List[DeviceInfo]:
        return list(self._devices.values())

    async def list_online(self) -> List[DeviceInfo]:
        return [d for d in self._devices.values()
                if d.status == DeviceStatus.ONLINE]

    async def list_by_type(self, device_type: DeviceType) -> List[DeviceInfo]:
        return [d for d in self._devices.values()
                if d.device_type == device_type]

    async def list_by_owner(self, user_id: str) -> List[DeviceInfo]:
        return [d for d in self._devices.values()
                if d.owner_user_id == user_id]

    async def search(self, query: str) -> List[DeviceInfo]:
        q = query.lower()
        return [
            d for d in self._devices.values()
            if q in d.name.lower()
            or q in (d.hostname or "").lower()
            or q in (d.ip_address or "").lower()
            or any(q in tag.lower() for tag in d.tags)
        ]

    async def sweep_stale(self, timeout_s: int = 90) -> List[str]:
        stale_ids = []
        for device in self._devices.values():
            if device.status == DeviceStatus.ONLINE and device.is_stale(timeout_s):
                await self.set_status(device.device_id, DeviceStatus.OFFLINE)
                stale_ids.append(device.device_id)
                log.warning("registry.device_stale", device_id=device.device_id)
        return stale_ids

    def on_change(self, callback) -> None:
        self._listeners.append(callback)

    async def _notify(self, event: str, device: DeviceInfo) -> None:
        for cb in self._listeners:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event, device)
                else:
                    cb(event, device)
            except Exception as e:
                log.error("registry.listener_error", error=str(e))


device_registry = DeviceRegistry()