Control your Apogee Duet (USB) on Linux!
===

This Python application and the Apogee Duet (USB) speak the same language :smile:

The interface works great out of the box using ALSA for recording and playing. However, there is no official way to control the settings like on macOS — this project speaks directly to the device over USB control transfers and wraps it in a modern, dark-themed control panel.

![Inputs page](docs/screenshots/inputs.png)
![Outputs page](docs/screenshots/outputs.png)
![Mixer page](docs/screenshots/mixer.png)

Features
---
- **Inputs**: gain faders with exact dB readouts, input type selection (+4dBu / -10dBV / Microphone / Instrument), phantom power, phase, soft limit and grouping. Line-level inputs correctly hide the gain control, since they bypass the preamp.
- **Outputs**: speaker and headphone levels with mute, dim, mono and output source selection.
- **Mixer**: per-channel level, pan (double-click to recentre), mute and solo for both inputs, the software return and the master bus.
- Every control talks to the device immediately; fader drags are throttled (~16 writes/s) so the UI stays fluid without flooding the USB bus.
- Failed USB control transfers are retried once automatically (the Duet drops roughly one transfer in a thousand).
- Graceful offline page when no Duet is attached.

Requirements
---
```sh
# Ubuntu / Debian
sudo apt install python3-usb python3-wxgtk4.0

# Arch Linux
sudo pacman -S python-pyusb wxpython
```

USB access without sudo
---
Running as root is not recommended. Give your user access to the device once with a udev rule:

```sh
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0c60", ATTR{idProduct}=="0016", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/90-apogee-duet.rules
sudo udevadm control --reload-rules
```

Unplug and replug the Duet (or run `sudo udevadm trigger`).

How to use
---
```sh
./take_control.py
```

The panel reads the full device state on startup, so it always opens in sync with the hardware. Changes made with the physical knob are not reflected back yet (see below).

Things to improve
---
1. Update controls when a setting is changed physically (using the knob or touchpads).
1. Add support for changing the assigned functions of the touchpads.

Credits
---
Forked from [fdgonthier/apogee-control](https://github.com/fdgonthier/apogee-control), with the protocol layer hardened and the GUI rebuilt as a custom-drawn wxPython panel.
