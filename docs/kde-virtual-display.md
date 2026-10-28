# KDE virtual displays (Plasma Wayland)

UU's IDD feature — 超级屏 (Super Screen), 防窥/隐私屏 and 关闭被控端显示屏 (turn the
controlled display off) — needs an extra output that the host can create and
then make the only active one. On GNOME UUWay creates a Mutter
`RecordVirtual` monitor. This branch does the same on Plasma.

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
4. UU's `SetDisplayConfig` arrives as the internal `0x55520001` topology
   request. When it selects the virtual output the module enables and positions
   it at the origin, makes it primary, and only then disables the physical
   outputs, so a desktop always keeps an active output and the panel goes dark.
   Replaying the saved physical layout restores the journalled KScreen layout.
5. The pre-change layout is journalled to `virtual-layout-recovery.json`. The
   physical outputs are restored when UU removes the display, when the bridge
   or its video track goes away, and by `ExecStopPost --recover` after a crash,
   so a failed session cannot leave the panel dark.

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

## Verification so far

* Virtual output creation, placement, exclusive switch (physical panel dark,
  desktop on the virtual output) and restore all work on Plasma 6.8 / KWin 6.8.
* `uuway doctor` reports no failures with the new backend.
* End-to-end remote verification is still open: after the exclusive switch UU's
  own capture of the virtual screen did not start (the remote client stayed
  black and the server exited), so the capture/owner hand-off needs more work.
