# KDE display-off support (Plasma Wayland)

UU's 关闭被控端显示屏 (turn the controlled display off) uses an IDD virtual
monitor to keep the remote desktop available while physical outputs are
disabled. On Plasma Wayland, UUWay creates a KWin virtual output through the
ScreenCast portal, captures it through PipeWire and applies its layout through
KScreen. This document covers display-off support on Plasma 6.8 / KWin 6.8.

## How it works

1. `KScreenVirtualDisplays.create()` opens a ScreenCast portal session with
   `types=VIRTUAL` (`4`). Plasma 6.8's portal asks KWin for a virtual output
   (`stream_virtual_output`) and returns its PipeWire node, size and the usual
   restore token. Authorization runs separately before enabling the feature;
   ticking "allow without asking" stores the grant in the portal permission
   store, which unattended remote use needs. The live display service requires
   a saved token and bounds each portal request to two seconds.
2. The new output appears in KScreen as `Virtual-virtual-xdp-kde-uuway` with a
   single mode. The module enables it, places it beside the physical layout and
   keeps the previous primary output primary, so the monitor order the display
   ABI reports to UU stays stable.
3. The output is reported to UU as `virtual=true` and its stream node is added
   to the `inspect` reply. The capture broker captures a virtual output from
   that node (`mutter-direct-capture.py --node`); there is no portal grant to
   restore for an output the portal itself created.
   Windows display names and device paths are assigned by connector and stay
   stable when physical outputs disappear from the active monitor list.
   The broker requests the monitor's actual size through `UURB_CAPTURE_SIZE`
   so PipeWire negotiates frames matching the Windows display configuration.
   KDE streams request separate cursor metadata. The producer reads standard
   SPA cursor updates and publishes them through the per-display and USER32
   cursor snapshots; the video frames contain no embedded cursor.
4. UU's `SetDisplayConfig` arrives as the internal `0x55520001` topology
   request. When it selects the virtual output the module enables and positions
   it at the origin, makes it primary, and only then disables the physical
   outputs, so a desktop always keeps an active output and the panel goes dark.
   Replaying the saved physical layout restores the journalled KScreen layout.
   Restore requests work with and without `SAVE_TO_DATABASE`. Physical-only
   restoration closes unselected virtual outputs. Requests containing both
   physical and owned virtual paths restore the physical layout and retain the
   selected virtual outputs.
5. The pre-change layout is journalled to `virtual-layout-recovery.json`. The
   physical outputs are restored when UU removes the display, when the bridge
   or its video track goes away, and by `ExecStopPost --recover` after a crash,
   so a failed session cannot leave the panel dark.
6. The supervisor writes the current KScreen input mapping to the private
   `input-map.json` in its runtime directory. The native input worker reloads
   atomic file updates while retaining its authenticated connection and uinput
   devices. Adding, removing or selecting a virtual output keeps UU running.
7. UU stores the display-off selection in `private_screen`: `1` is
   `PS_NOT_SET`, `2` is `PS_NORMAL_PRIVACY`, and `3` is `PS_ENHANCED_PRIVACY`.
   The service reads the current setting during connection setup and watches
   subsequent changes. Enabling display-off selects virtual slot zero;
   disabling it removes that output and restores the physical layout.
   UU may retain its virtual-screen label after the physical output is restored;
   the adapter leaves the client's screen selection under client control.
   Standalone virtual slots retain their explicit topology controls.

## Known limitations

* The portal fixes the virtual output's resolution when the grant is created
  (the dialog's resolution picker). KWin has no runtime mode change for it, so
  the module advertises only the real mode. Requests for another topology mode
  fail before changing the physical outputs. UU's IDD mode catalogue is accepted
  as driver configuration; it does not add unsupported KWin modes.
* KDE's display configuration is a real layout change, not a temporary Mutter
  transaction; there is no 30-second confirmation dialog. The journal and the
  recovery file are the rollback mechanism.
* Rotation, mirroring and Intel-only encoding keep their existing limits.

## Authorization

Before enabling virtual display reconfiguration, run this command in the KDE
session and select the matching resolution and remembered authorization:

```sh
python3 scripts/authorize-kde-virtual-display.py \
  --state "$HOME/.local/state/uurb/virtual-portal-state.json" \
  --width 2560 --height 1440
```

The command creates and closes a virtual output while keeping physical outputs
enabled. It rejects a different size before saving the token. The requested
dimensions must match the mode UU submits when entering its virtual topology.

## Verification

* Virtual output creation, placement, exclusive switch (physical panel dark,
  desktop on the virtual output) and restore all work on Plasma 6.8 / KWin 6.8.
* Independent 2560×1440 capture delivers DMA-BUF frames and imports every
  captured frame into Vulkan without mapping pixels to the CPU.
* Without a saved authorization, IDD discovery is rejected while physical
  display inspection remains available.
* A live uinput test adds and removes a real Portal output, verifies the map
  changes from one output to two and back to one, and confirms that the input
  connection and bridge runtime survive both stable layout changes.
* The live Win32 lifecycle probe exercises exclusive mode, mixed-output
  restoration, verify-only requests and physical-only restoration with the
  actual `0x14a0` and `0x10a0` flags, using the production display adapter and
  the real KDE display service.
* Remote-client tests confirm that ordinary connections use DP-3, enabling
  display-off moves the desktop to the virtual output and turns off the
  physical panel, and disabling it restores the panel. Continuous video,
  keyboard and mouse work during display-off; separate cursor metadata avoids
  duplicate cursors.
* With display-off selected, disconnecting and reconnecting automatically uses
  the virtual output, keeps the physical panel off and preserves remote input.
