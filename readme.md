### Requirements
 - **never** use Windows
 - Linux (e.g. Ubuntu) or MacOS laptop with working bluetooth module
 - Python 3.9 or 3.10 installed

### How to
 - clone the repository (or just download it)
 - create the virtualenv
 - install necessary packages to the virtualenv
 - **take your laptop and come closer to the JK BMS**
 - run `jikong.py`
 - you will see list of available bluetooth devices and their addresses, copy the bms' address and paste it to the prompt
 - see the output

### Example
```shell
$ git clone <url_here>
$ python -m venv ~/venv_jk
$ source ~/venv_jk/bin/activate
(venv_jk)$ pip install -U pip setuptools wheel bleak==0.20.2
(venv_jk)$ python jikong.py

BT Discovery:
BT Device   None   address=23DD2457-9221-BE9A-19CE-F33B880F2206
BT Device   JK_B2A24S15P   address=072B3AC3-5133-A9EF-84D8-595BF0DF53F8
BT Device   Amazfit GTR   address=ABFAE28B-0DC5-FFD4-CB63-1103FD6F3FCB
BT Device   Mi Smart Band 5   address=C95B9C88-810D-3C80-3504-8D03B7F44347


Enter JK BMS addr (see list above): 072B3AC3-5133-A9EF-84D8-595BF0DF53F8
model: JK_B2A24S15P
hw: 11.XW
sf: 11.26
device_name: JK_B2A24S15P
device_passcode: 1234
manufactured: 230430
sn: 2120<hidden>
passcode: 0000
setup_passcode: 123456
```

Thanks to the https://github.com/fl4p/batmon-ha, I've used that code to "compile" this JK BMS password retrieval tool
