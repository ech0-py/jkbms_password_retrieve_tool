"""

https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jk02.py
https://github.com/jblance/jkbms
https://github.com/sshoecraft/jktool/blob/main/jk_info.c
https://github.com/syssi/esphome-jk-bms
https://github.com/PurpleAlien/jk-bms_grafana


fix connection abort:
- https://github.com/hbldh/bleak/issues/631 (use bluetoothctl !)
- https://github.com/hbldh/bleak/issues/666

"""
import asyncio
from collections import defaultdict
from typing import List, Callable, Dict

from bt import BtBms, bt_discovery, get_logger


def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF


def read_str(buf, offset, encoding='utf-8'):
    return buf[offset:buf.index(0x00, offset)].decode(encoding=encoding)


def to_hex_str(data):
    return " ".join(map(lambda b: hex(b)[2:], data))


def _jk_command(address, value: list):
    n = len(value)
    assert n <= 13, "val %s too long" % value
    frame = bytes([0xAA, 0x55, 0x90, 0xEB, address, n])
    frame += bytes(value)
    frame += bytes([0] * (13 - n))
    frame += bytes([calc_crc(frame)])
    return frame


MIN_RESPONSE_SIZE = 300
MAX_RESPONSE_SIZE = 320


class JKBt(BtBms):
    CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        if kwargs.get('psk'):
            self.logger.warning('JK usually does not use a pairing PIN')
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._resp_table = {}
        self.num_cells = None
        self._callbacks: Dict[int, List[Callable[[bytes], None]]] = defaultdict(List)
        self.char_handle_notify = self.CHAR_UUID
        self.char_handle_write = self.CHAR_UUID

    def _buffer_crc_check(self):
        crc_comp = calc_crc(self._buffer[0:MIN_RESPONSE_SIZE - 1])
        crc_expected = self._buffer[MIN_RESPONSE_SIZE - 1]
        if crc_comp != crc_expected:
            self.logger.debug("crc check failed, %s != %s, %s", crc_comp, crc_expected, self._buffer)
        return crc_comp == crc_expected

    def _notification_handler(self, sender, data):
        HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])

        if data[0:4] == HEADER:  # and len(self._buffer)
            self.logger.debug("header, clear buf %s", self._buffer)
            self._buffer.clear()

        self._buffer += data

        self.logger.debug("bms msg(%d) (buf%d): %s\n", len(data), len(self._buffer), to_hex_str(data))

        if len(self._buffer) >= MIN_RESPONSE_SIZE:
            if len(self._buffer) > MAX_RESPONSE_SIZE:
                self.logger.warning('buffer longer than expected %d %s', len(self._buffer), self._buffer)

            crc_ok = self._buffer_crc_check()

            if not crc_ok and HEADER in self._buffer:
                idx = self._buffer.index(HEADER)
                self.logger.debug("crc check failed, header at %d, discarding start of %s", idx, self._buffer)
                self._buffer = self._buffer[idx:]
                crc_ok = self._buffer_crc_check()

            if not crc_ok:
                self.logger.error("crc check failed, discarding buffer %s", self._buffer)
            else:
                self._decode_msg(bytearray(self._buffer))
            self._buffer.clear()

    def _decode_msg(self, buf):
        resp_type = buf[4]
        self.logger.debug('got response %d (len%d)', resp_type, len(buf))
        self._resp_table[resp_type] = buf
        self._fetch_futures.set_result(resp_type, self._buffer[:])
        callbacks = self._callbacks.get(resp_type, None)
        if callbacks:
            for cb in callbacks:
                cb(buf)

    async def connect(self, timeout=20):
        """
        Connecting JK with bluetooth appears to require a prior bluetooth scan and discovery, otherwise the connectiong fails with
        `[org.bluez.Error.Failed] Software caused connection abort`. Maybe the scan triggers some wake up?
        :param timeout:
        :return:
        """

        try:
            await super().connect(timeout=6)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", str(e) or type(e))
            await self._connect_with_scanner(timeout=timeout)

        # there might be 2 chars with same uuid (weird?), one for notify/read and one for write
        # https://github.com/fl4p/batmon-ha/issues/83
        self.char_handle_notify = self.characteristic_uuid_to_handle(self.CHAR_UUID, 'notify')
        self.char_handle_write = self.characteristic_uuid_to_handle(self.CHAR_UUID, 'write')

        self.logger.debug('char_handle_notify=%s, char_handle_write=%s', self.char_handle_notify,
                          self.char_handle_write)

        await self.start_notify(self.char_handle_notify, self._notification_handler)

        await self._q(cmd=0x97, resp=0x03)  # device info

    async def disconnect(self):
        await self.client.stop_notify(self.char_handle_notify)
        await super().disconnect()

    async def _q(self, cmd, resp):
        with self._fetch_futures.acquire(resp):
            frame = _jk_command(cmd, [])
            self.logger.debug("write %s", frame)
            await self.client.write_gatt_char(self.char_handle_write, data=frame)
            return await self._fetch_futures.wait_for(resp, self.TIMEOUT)

    def fetch_device_info(self):
        # https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jkabstractprotocol.py
        # https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms_ble/jk_bms_ble.cpp#L1059
        buf = self._resp_table[0x03]
        structure = {
            'skip_header': 4,
            'skip_rec_type': 1,
            'skip_rec_counter': 1,
            'model': 16,
            'hw': 8,
            'sf': 8,
            'skip_uptime': 4,
            'skip_power': 4,
            'device_name': 16,
            'device_passcode': 16,
            'manufactured': 8,
            'sn': 11,
            'passcode': 5,
            'skip_user_data': 16,
            'setup_passcode': 16,
        }
        for _type, _len in structure.items():
            if not _type.startswith('skip'):
                val = buf[:_len].rstrip(b'\x00').decode(errors='ignore')
                print(f'{_type}: {val}')
            buf = buf[_len:]


async def main():
    print()
    await bt_discovery()
    print('\n')
    mac_address = input('Enter JK BMS addr (see list above): ')
    bms = JKBt(mac_address, name='jk', verbose_log=False)

    async with bms:
        bms.fetch_device_info()

if __name__ == '__main__':
    asyncio.run(main())
