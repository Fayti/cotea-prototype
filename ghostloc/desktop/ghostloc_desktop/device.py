"""Lien avec l'iPhone : appairage, mode développeur, image développeur, simulation GPS.

iOS 17.4+ : tunnel en espace utilisateur (pas de droits administrateur) puis service DVT
LocationSimulation, qui n'agit que tant que la connexion reste ouverte.
iOS 16 et moins : service com.apple.dt.simulatelocation via lockdown.
"""

import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from typing import Callable, Optional

from packaging.version import Version

log = logging.getLogger(__name__)

NO_SERVICE_HELP = {
    "win32": "Service Apple introuvable. Installe « Apple Devices » (Microsoft Store) ou iTunes, puis relance GhostLoc.",
    "darwin": "Service Apple indisponible. Redémarre le Mac puis relance GhostLoc.",
    "linux": "usbmuxd introuvable. Installe-le (sudo apt install usbmuxd) puis relance GhostLoc.",
}


class LinkError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


StatusCallback = Callable[[str, str], None]


class IPhoneLink:
    def __init__(self) -> None:
        self.serial: Optional[str] = None
        self.name = ""
        self.ios = ""
        self.connection = ""
        self._stack: Optional[AsyncExitStack] = None
        self._lockdown = None
        self._set = None
        self._clear = None

    @property
    def ready(self) -> bool:
        return self._set is not None

    async def attached_serials(self) -> list[str]:
        from pymobiledevice3 import usbmux

        try:
            return [d.serial for d in await usbmux.list_devices()]
        except Exception:
            return []

    async def connect(self, on_status: StatusCallback) -> None:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import (
            AlreadyMountedError,
            PairingDialogResponsePendingError,
            PasswordRequiredError,
            UserDeniedPairingError,
            UserspaceTunnelUnavailableError,
        )
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobile_image_mounter import auto_mount

        await self.close()
        try:
            devices = await usbmux.list_devices()
        except Exception as e:
            raise LinkError("no_service", NO_SERVICE_HELP.get(sys.platform, NO_SERVICE_HELP["win32"])) from e
        if not devices:
            raise LinkError("none", "Branche ton iPhone en USB et déverrouille-le.")
        device = sorted(devices, key=lambda d: not d.is_usb)[0]
        self.serial = device.serial
        self.connection = "USB" if device.is_usb else "Wi-Fi"

        on_status("pairing", "Déverrouille ton iPhone et touche « Se fier à cet ordinateur ».")
        try:
            lockdown = await create_using_usbmux(serial=device.serial, pair_timeout=120)
        except PasswordRequiredError as e:
            raise LinkError("locked", "Déverrouille ton iPhone puis réessaie.") from e
        except UserDeniedPairingError as e:
            raise LinkError("denied", "Tu as refusé « Se fier ». Débranche, rebranche et accepte.") from e
        except PairingDialogResponsePendingError as e:
            raise LinkError("pairing", "Touche « Se fier » sur ton iPhone puis réessaie.") from e

        stack = AsyncExitStack()
        stack.push_async_callback(lockdown.close)
        self._lockdown = lockdown
        self.name = lockdown.all_values.get("DeviceName") or "iPhone"
        self.ios = lockdown.product_version
        version = Version(self.ios)

        try:
            if version >= Version("16.0") and not await lockdown.get_developer_mode_status():
                raise LinkError(
                    "devmode",
                    "Active le mode développeur : Réglages › Confidentialité et sécurité › Mode développeur, "
                    "puis redémarre l'iPhone et confirme « Activer ».",
                )

            on_status("preparing", "Préparation de l'iPhone (première fois : jusqu'à 1 minute)…")
            if version >= Version("17.0"):
                from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
                from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
                from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

                try:
                    rsd = await stack.enter_async_context(
                        UserspaceRsdTunnel(serial=device.serial, remotepairing_fallback=False)
                    )
                except UserspaceTunnelUnavailableError as e:
                    raise LinkError("unsupported", "iOS 17.0 à 17.3 non pris en charge : mets ton iPhone à jour.") from e
                try:
                    await auto_mount(rsd)
                except AlreadyMountedError:
                    pass
                dvt = await stack.enter_async_context(DvtProvider(rsd))
                simulation = await stack.enter_async_context(LocationSimulation(dvt))
                self._set, self._clear = simulation.set, simulation.clear
            else:
                from pymobiledevice3.services.simulate_location import DtSimulateLocation

                try:
                    await auto_mount(lockdown)
                except AlreadyMountedError:
                    pass
                simulation = DtSimulateLocation(lockdown)
                self._set, self._clear = simulation.set, simulation.clear
        except LinkError:
            await stack.aclose()
            self._lockdown = None
            raise
        except Exception as e:
            await stack.aclose()
            self._lockdown = None
            log.exception("préparation impossible")
            raise LinkError("error", f"Préparation impossible : {e}") from e
        self._stack = stack

    async def reveal_developer_mode(self) -> None:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.amfi import AmfiService

        lockdown = self._lockdown or await create_using_usbmux(serial=self.serial)
        try:
            await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
        finally:
            if lockdown is not self._lockdown:
                await lockdown.close()

    async def set(self, lat: float, lng: float) -> None:
        if not self._set:
            raise LinkError("none", "iPhone non connecté.")
        await self._set(lat, lng)

    async def clear(self) -> None:
        if self._clear:
            await self._clear()

    async def close(self) -> None:
        self._set = self._clear = None
        if self._stack:
            stack, self._stack = self._stack, None
            try:
                await asyncio.wait_for(stack.aclose(), timeout=5)
            except Exception:
                log.debug("fermeture du lien", exc_info=True)
        self._lockdown = None


class DemoLink(IPhoneLink):
    """iPhone simulé pour essayer l'interface sans appareil (--demo)."""

    async def attached_serials(self) -> list[str]:
        return ["DEMO"]

    async def connect(self, on_status: StatusCallback) -> None:
        on_status("preparing", "Préparation de l'iPhone de démonstration…")
        await asyncio.sleep(0.5)
        self.serial, self.name, self.ios, self.connection = "DEMO", "iPhone de démo", "18.6", "USB"
        self.positions: list[tuple[float, float]] = []

        async def _set(lat: float, lng: float) -> None:
            self.positions.append((lat, lng))

        async def _clear() -> None:
            self.positions.append((0.0, 0.0))

        self._set, self._clear = _set, _clear

    async def reveal_developer_mode(self) -> None:
        return None
