import asyncio
import re
import subprocess
import time
import logging
from typing import Callable, List, Dict, Union, Tuple

from bleak import BleakClient, BleakScanner


NameType = Union[str, int, Tuple[Union[str, int]]]


def get_logger(verbose=False):
    log_format = '%(asctime)s %(levelname)s [%(module)s] %(message)s'
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt='%H:%M:%S')
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    return logger


class FuturesPool:
    """
    Manage a collection of named futures.
    """
    def __init__(self):
        self._futures: Dict[str, asyncio.Future] = {}

    def acquire(self, name: NameType):
        if isinstance(name, tuple):
            tuple(self.acquire(n) for n in name)
            return FutureContext(name, pool=self)

        assert name not in self._futures, "already waiting for %s" % name
        fut = asyncio.Future()
        self._futures[name] = fut
        return FutureContext(name, pool=self)

    def set_result(self, name, value):
        fut = self._futures.get(name, None)
        if fut:
            # if fut.done():
            #    print('future %s already done' % name)
            fut.set_result(value)

    def clear(self):
        for fut in self._futures.values():
            fut.cancel()
        self._futures.clear()

    def remove(self, name):
        if isinstance(name, tuple):
            return tuple(self.remove(n) for n in name)
        self._futures.pop(name, None)

    async def wait_for(self, name: NameType, timeout):
        if isinstance(name, tuple):
            tasks = [self.wait_for(n, timeout) for n in name]
            return await asyncio.gather(*tasks, return_exceptions=False)

        try:
            return await asyncio.wait_for(self._futures.get(name), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise
        finally:
            self.remove(name)


class FutureContext:
    def __init__(self, name: NameType, pool: FuturesPool):
        self.name = name
        self.pool = pool

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pool.remove(self.name)


async def bt_discovery():
    print('BT Discovery:')
    devices = await BleakScanner.discover()
    if not devices:
        print(' - no devices found - ')
    for d in devices:
        print(f"BT Device   {d.name}   address={d.address}")


def bleak_version():
    try:
        import bleak
        return bleak.__version__
    except:
        from importlib.metadata import version
        return version('bleak')


def bt_stack_version():
    try:
        # get BlueZ version
        p = subprocess.Popen(["bluetoothctl", "--version"], stdout=subprocess.PIPE)
        out, _ = p.communicate()
        s = re.search(b"(\\d+).(\\d+)", out.strip(b"'"))
        bluez_version = tuple(map(int, s.groups()))
        return 'bluez-v%i.%i' % bluez_version
    except:
        return '? (%s)' % BleakClient.__name__


class BtBms:
    def __init__(self, address: str, name: str, keep_alive=False, psk=None, adapter=None, verbose_log=False):
        self.address = address
        self.name = name
        self.keep_alive = keep_alive
        self.verbose_log = verbose_log
        self.logger = get_logger(verbose_log)
        self._fetch_futures = FuturesPool()
        self._psk = psk
        self._connect_time = 0

        if address.startswith('test_'):
            pass
        else:
            kwargs = {}
            if psk:
                try:
                    import bleak.backends.bluezdbus.agent
                except ImportError:
                    self.logger.warn("this bleak version has no pairing agent, pairing with a pin will likely fail!")

            self._adapter = adapter
            if adapter:  # hci0, hci1 (BT adapter hardware)
                kwargs['adapter'] = adapter

            self.client = BleakClient(address,
                                      handle_pairing=bool(psk),
                                      disconnected_callback=self._on_disconnect,
                                      **kwargs
                                      )

    async def start_notify(self, char_specifier, callback: Callable[[int, bytearray], None], **kwargs):
        if not isinstance(char_specifier, list):
            char_specifier = [char_specifier]
        exception = None
        for cs in char_specifier:
            try:
                await self.client.start_notify(cs, callback, **kwargs)
                return cs
            except Exception as e:
                exception = e
        await enumerate_services(self.client, self.logger)
        raise exception

    def characteristic_uuid_to_handle(self, uuid: str, property: str) -> Union[str, int]:
        for service in self.client.services:
            for char in service.characteristics:
                if char.uuid == uuid and property in char.properties:
                    return char.handle
        return uuid

    def _on_disconnect(self, client):
        if self.keep_alive and self._connect_time:
            self.logger.warning('BMS %s disconnected after %.1fs!', self.__str__(), time.time() - self._connect_time)

        try:
            self._fetch_futures.clear()
        except Exception as e:
            self.logger.warning('error clearing futures pool: %s', str(e) or type(e))

    async def _connect_client(self, timeout):
        await self.client.connect(timeout=timeout)
        if self.verbose_log:
            await enumerate_services(self.client, logger=self.logger)
        self._connect_time = time.time()
        if self._psk:
            def get_passkey(device: str, pin, passkey):
                if pin:
                    self.logger.info(f"Device {device} is displaying pin '{pin}'")
                    return True

                if passkey:
                    self.logger.info(f"Device {device} is displaying passkey '{passkey:06d}'")
                    return True

                self.logger.info(f"Device {device} asking for psk, giving '{self._psk}'")
                return str(self._psk) or None

            self.logger.debug("Pairing %s using psk '%s'...", self._psk)
            res = await self.client.pair(callback=get_passkey)
            if not res:
                self.logger.error("Pairing failed!")

    @property
    def is_connected(self):
        return self.client.is_connected

    async def connect(self, timeout=20):
        """
        Establish a BLE connection
        :param timeout:
        :return:
        """
        await self._connect_client(timeout=timeout)

    async def _connect_with_scanner(self, timeout=20):
        """
        Starts a bluetooth discovery and tries to establish a BLE connection with back off.
         This fixes connection errors for some BMS (jikong). Use instead of connect().

        :param timeout:
        :return:
        """
        import bleak
        scanner_kw = {}
        if self._adapter:
            scanner_kw['adapter'] = self._adapter
        scanner = bleak.BleakScanner(**scanner_kw)
        self.logger.debug("starting scan")
        await scanner.start()

        attempt = 1
        while True:
            try:
                discovered = set(b.address for b in scanner.discovered_devices)
                if self.client.address not in discovered:
                    raise Exception('Device %s not discovered. Make sure it in range and is not being controled by '
                                    'another application. (%s)' % (self.client.address, discovered))

                self.logger.debug("connect attempt %d", attempt)
                await self._connect_client(timeout=timeout / 2)
                break
            except Exception as e:
                await self.client.disconnect()
                if attempt < 8:
                    self.logger.debug('retry %d after error %s', attempt, e)
                    await asyncio.sleep(0.2 * (1.5 ** attempt))
                    attempt += 1
                else:
                    await scanner.stop()
                    raise

        await scanner.stop()

    async def disconnect(self):
        await self.client.disconnect()
        self._fetch_futures.clear()

    def __str__(self):
        return f'{self.__class__.__name__}({self.client.address})'

    async def __aenter__(self):
        # print("enter")
        if self.keep_alive and self.is_connected:
            return
        await self.connect()

    async def __aexit__(self, *args):
        # print("exit")
        if self.keep_alive:
            return
        if self.client.is_connected:
            await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    def set_keep_alive(self, keep):
        if keep:
            self.logger.info("BMS %s keep alive enabled", self.__str__())
        self.keep_alive = keep

    def debug_data(self):
        return None


async def enumerate_services(client: BleakClient, logger):
    for service in client.services:
        logger.info(f"[Service] {service}")
        for char in service.characteristics:
            if "read" in char.properties:
                try:
                    value = bytes(await client.read_gatt_char(char.uuid))
                    logger.info(
                        f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {value}"
                    )
                except Exception as e:
                    logger.error(
                        f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {e}"
                    )

            else:
                value = None
                logger.info(
                    f"\t[Characteristic] {char} ({','.join(char.properties)}), Value: {value}"
                )

            for descriptor in char.descriptors:
                try:
                    value = bytes(
                        await client.read_gatt_descriptor(descriptor.handle)
                    )
                    logger.info(f"\t\t[Descriptor] {descriptor}) | Value: {value}")
                except Exception as e:
                    logger.error(f"\t\t[Descriptor] {descriptor}) | Value: {e}")
