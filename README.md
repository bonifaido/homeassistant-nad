# NAD Receiver for Home Assistant

Control compatible NAD receivers locally through Home Assistant.

## Features

- Media player controls for power, volume, mute, and source selection where supported.
- Receiver-specific settings appear as number, select, switch, or sensor entities when supported by the model.
- C328 controls include AutoSense, AutoStandby, Bass EQ, and VFD brightness (0-3).
- C328 source selection: TV, PHONO, COAX1, COAX2, OPT1, OPT2, STREAM, and BT.
- C328 volume defaults to -80 dB through +12 dB. The slider range can be changed in the integration options.

## Installation

### HACS

[![Open NAD in Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bonifaido&repository=homeassistant-nad&category=integration)

Install **NAD** from HACS under **Integrations**. If it is not listed, add `https://github.com/bonifaido/homeassistant-nad` as a custom repository with the **Integration** category.

### Manual

Copy `custom_components/nad` into your Home Assistant `config/custom_components` directory, then restart Home Assistant.

## Setup

In Home Assistant, open **Settings > Devices & services > Add integration**, then search for **NAD**. Choose the connection method that matches your receiver:

- **Serial**: a serial port available to Home Assistant.
- **Telnet**: a network host and port. Use this for raw TCP serial bridges such as ser2net; enter the bridge's configured port.
- **TCP**: the receiver's native NAD TCP connection.

For a network bridge, Home Assistant must be able to reach its host and port. If `.local` names do not resolve from your Home Assistant installation, use the receiver or bridge's IP address.

## ser2net example

For a C328 connected to `/dev/ttyUSB0`, a minimal ser2net 4 YAML connection is:

```yaml
%YAML 1.1
---
connection: &nad-c328
	accepter: tcp,4000
	enable: on
	connector: serialdev,
						 /dev/ttyUSB0,
						 115200n81,local
	options:
		kickolduser: true
```

Change `/dev/ttyUSB0` to the serial device used by your bridge. Configure the NAD integration with the bridge's IP address and port `4000`, using **Telnet** (raw TCP). With `kickolduser` enabled, a new connection disconnects the existing client.

## Troubleshooting

- Confirm the selected connection method and port match the receiver or serial bridge.
- For a VM or container, confirm it can reach the receiver's LAN address and TCP port.
- Check the Home Assistant logs for connection errors. A new ser2net connection may disconnect an existing client if `kickolduser` is enabled.
