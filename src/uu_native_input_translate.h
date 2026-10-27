#ifndef UURB_NATIVE_INPUT_TRANSLATE_H
#define UURB_NATIVE_INPUT_TRANSLATE_H
/* v3 physical-input wire records are shared with the retired X11 transport.
 * Translation here has no X11, Wine, clipboard or RDP dependency. */
#include "x11_input_protocol.h"
#include <linux/input.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

struct uurb_native_events {
    struct input_event events[20];
    size_t count;
    int keyboard;
};
static inline void uurb_emit(struct uurb_native_events *out, unsigned type, unsigned code, int value)
{
    out->events[out->count++] = (struct input_event){.type = type, .code = code, .value = value};
}
static inline unsigned uurb_evdev_key(unsigned scan, unsigned extended, unsigned vk)
{
    if (vk == 0x13) return KEY_PAUSE;
    if (vk == 0x2c) return KEY_SYSRQ;
    if (!extended) {
        /* Linux input's legacy AT set-1 codes align only in this range. */
        return scan >= 1 && scan <= 88 && scan != 84 ? scan : 0;
    }
    switch (scan) {
    case 0x1c: return KEY_KPENTER;
    case 0x1d: return KEY_RIGHTCTRL;
    case 0x35: return KEY_KPSLASH;
    case 0x37: return KEY_SYSRQ;
    case 0x38: return KEY_RIGHTALT;
    case 0x47: return KEY_HOME;
    case 0x48: return KEY_UP;
    case 0x49: return KEY_PAGEUP;
    case 0x4b: return KEY_LEFT;
    case 0x4d: return KEY_RIGHT;
    case 0x4f: return KEY_END;
    case 0x50: return KEY_DOWN;
    case 0x51: return KEY_PAGEDOWN;
    case 0x52: return KEY_INSERT;
    case 0x53: return KEY_DELETE;
    case 0x5b: return KEY_LEFTMETA;
    case 0x5c: return KEY_RIGHTMETA;
    case 0x5d: return KEY_COMPOSE;
    default: return 0;
    }
}

/* KWin maps a uinput absolute pointer over the bounding box of every output
 * in logical pixels, while UU addresses Windows monitors at physical size.
 * Each entry pairs a Windows monitor rectangle with the share of the absolute
 * axes KWin gives that output; entry 0 is the primary monitor. */
#define UURB_OUTPUT_MAP_MAX 8u
#define UURB_ABSOLUTE_AXIS_MAX 65535
#define UURB_ABSOLUTE_SPAN 65536.0
struct uurb_output_rect { int32_t x, y, width, height; };
struct uurb_output_map {
    unsigned count;
    struct uurb_output_rect windows[UURB_OUTPUT_MAP_MAX], axis[UURB_OUTPUT_MAP_MAX];
};
/* "wx,wy,ww,wh,ax,ay,aw,ah;..." as written by desktop_backend.input_map(). */
static inline int uurb_output_map_parse(const char *text, struct uurb_output_map *map)
{
    memset(map, 0, sizeof(*map));
    while (*text) {
        if (map->count == UURB_OUTPUT_MAP_MAX) return 0;
        struct uurb_output_rect *w = &map->windows[map->count], *a = &map->axis[map->count];
        int used = 0;
        if (sscanf(text, "%d,%d,%d,%d,%d,%d,%d,%d%n", &w->x, &w->y, &w->width, &w->height,
                   &a->x, &a->y, &a->width, &a->height, &used) != 8) return 0;
        if (w->x < -32768 || w->x > 32767 || w->y < -32768 || w->y > 32767 ||
            w->width < 2 || w->width > 4096 || w->height < 2 || w->height > 4096 ||
            a->x < 0 || a->y < 0 || a->width < 1 || a->height < 1 ||
            a->x + a->width > UURB_ABSOLUTE_AXIS_MAX + 1 || a->y + a->height > UURB_ABSOLUTE_AXIS_MAX + 1) return 0;
        ++map->count;
        text += used;
        if (*text == ';') {
            if (!*++text) return 0;
        } else if (*text) {
            return 0;
        }
    }
    return map->count > 0;
}
static inline double uurb_clamp_unit(double value)
{
    return value < 0 ? 0 : value > 1 ? 1 : value;
}
/* SendInput semantics: VIRTUALDESK coordinates span the union of all
 * monitors, otherwise the primary monitor.  The addressed pixel is found on
 * its Windows monitor (or the nearest one) and moved to that output's share
 * of the KWin axes. */
static inline void uurb_output_map_apply(const struct uurb_output_map *map, int virtual_desk, int *x, int *y)
{
    struct uurb_output_rect area = map->windows[0];
    if (virtual_desk) {
        int32_t right = area.x + area.width, bottom = area.y + area.height;
        for (unsigned i = 1; i < map->count; ++i) {
            const struct uurb_output_rect *r = &map->windows[i];
            if (r->x < area.x) area.x = r->x;
            if (r->y < area.y) area.y = r->y;
            if (r->x + r->width > right) right = r->x + r->width;
            if (r->y + r->height > bottom) bottom = r->y + r->height;
        }
        area.width = right - area.x; area.height = bottom - area.y;
    }
    double px = area.x + *x * (double)area.width / UURB_ABSOLUTE_SPAN;
    double py = area.y + *y * (double)area.height / UURB_ABSOLUTE_SPAN;
    unsigned best = 0;
    double best_distance = -1;
    for (unsigned i = 0; i < map->count; ++i) {
        const struct uurb_output_rect *r = &map->windows[i];
        double dx = px < r->x ? r->x - px : px >= r->x + r->width ? px - (r->x + r->width) + 1 : 0;
        double dy = py < r->y ? r->y - py : py >= r->y + r->height ? py - (r->y + r->height) + 1 : 0;
        double distance = dx * dx + dy * dy;
        if (best_distance < 0 || distance < best_distance) { best = i; best_distance = distance; }
    }
    const struct uurb_output_rect *w = &map->windows[best], *a = &map->axis[best];
    double u = uurb_clamp_unit((px - w->x) / w->width), v = uurb_clamp_unit((py - w->y) / w->height);
    int mx = (int)(a->x + u * (a->width - 1) + 0.5), my = (int)(a->y + v * (a->height - 1) + 0.5);
    *x = mx > UURB_ABSOLUTE_AXIS_MAX ? UURB_ABSOLUTE_AXIS_MAX : mx;
    *y = my > UURB_ABSOLUTE_AXIS_MAX ? UURB_ABSOLUTE_AXIS_MAX : my;
}

static inline int uurb_native_translate_mapped(const uurb_x11_input_event *in, const struct uurb_output_map *map,
                                               struct uurb_native_events *out)
{
    memset(out, 0, sizeof(*out));
    unsigned flags = in->flags;
    if (in->type == UURB_X11_INPUT_KEYBOARD) {
        if (flags & ~(UURB_KEYEVENTF_EXTENDED | UURB_KEYEVENTF_KEYUP | UURB_KEYEVENTF_SCANCODE) ||
            in->x || in->y || in->data) return 0;
        unsigned key = uurb_evdev_key(in->scan_code, flags & UURB_KEYEVENTF_EXTENDED, in->virtual_key);
        if (!key) return 0;
        out->keyboard = 1;
        uurb_emit(out, EV_KEY, key, !(flags & UURB_KEYEVENTF_KEYUP));
    } else if (in->type == UURB_X11_INPUT_MOUSE) {
        /* Without an output map the compositor receives the normalized range
         * unchanged, which matches only when its layout equals the Windows
         * one. A map moves each absolute point onto its output's share. */
        const unsigned move = 1, absolute = 0x8000, wheel = 0x800, hwheel = 0x1000, virtual_desk = 0x4000;
        const unsigned xdown = 0x80, xup = 0x100;
        if (flags & ~0xf9ffu || in->virtual_key || in->scan_code) return 0;
        if ((flags & (wheel | hwheel)) && (flags & (xdown | xup))) return 0;
        if ((flags & wheel) && (flags & hwheel)) return 0;
        /* As SendInput does: without MOUSEEVENTF_MOVE the coordinates (and the
         * ABSOLUTE/VIRTUALDESK flags) are ignored and buttons act at the
         * current position; absolute positions past the edge are clamped. UU
         * sends a phone tap as an absolute move, then separate absolute
         * button-down/up events without MOVE. */
        if (flags & move) {
            if (flags & absolute) {
                int x = in->x < 0 ? 0 : in->x > 65535 ? 65535 : in->x;
                int y = in->y < 0 ? 0 : in->y > 65535 ? 65535 : in->y;
                if (map && map->count) uurb_output_map_apply(map, (flags & virtual_desk) != 0, &x, &y);
                uurb_emit(out, EV_ABS, ABS_X, x); uurb_emit(out, EV_ABS, ABS_Y, y);
            } else {
                if (in->x < -32768 || in->x > 32767 || in->y < -32768 || in->y > 32767) return 0;
                uurb_emit(out, EV_REL, REL_X, in->x); uurb_emit(out, EV_REL, REL_Y, in->y);
            }
        }
        const unsigned bits[] = {2, 4, 8, 16, 32, 64};
        const unsigned buttons[] = {BTN_LEFT, BTN_LEFT, BTN_RIGHT, BTN_RIGHT, BTN_MIDDLE, BTN_MIDDLE};
        for (unsigned i = 0; i < 6; ++i) if (flags & bits[i])
            uurb_emit(out, EV_KEY, buttons[i], !(i & 1));
        if (flags & (xdown | xup)) {
            if (!in->data || in->data > 3) return 0;
            for (unsigned i = 0; i < 2; ++i) if (in->data & (1u << i)) {
                if (flags & xdown) uurb_emit(out, EV_KEY, i ? BTN_EXTRA : BTN_SIDE, 1);
                if (flags & xup) uurb_emit(out, EV_KEY, i ? BTN_EXTRA : BTN_SIDE, 0);
            }
        } else if (flags & (wheel | hwheel)) {
            int delta = (int32_t)in->data;
            if (delta < -12000 || delta > 12000) return 0;
            if (delta) uurb_emit(out, EV_REL, flags & wheel ? REL_WHEEL_HI_RES : REL_HWHEEL_HI_RES, delta);
        } else if (in->data) return 0;
    } else return 0;
    uurb_emit(out, EV_SYN, SYN_REPORT, 0);
    return 1;
}
static inline int uurb_native_translate(const uurb_x11_input_event *in, struct uurb_native_events *out)
{
    return uurb_native_translate_mapped(in, NULL, out);
}

/* Windows and evdev high-resolution wheels both use 120 units per detent.
 * Preserve every fractional report; legacy axes are accumulated separately.
 * Call only after full-batch validation. Commit state after successful write. */
struct uurb_native_scroll { int vertical, horizontal; };
static inline void uurb_native_scroll_legacy(struct uurb_native_events *out, struct uurb_native_scroll *state)
{
    for (size_t i = 0; i < out->count; ++i) {
        struct input_event *event = &out->events[i];
        if (event->type != EV_REL || (event->code != REL_WHEEL_HI_RES && event->code != REL_HWHEEL_HI_RES)) continue;
        int vertical = event->code == REL_WHEEL_HI_RES;
        int *remainder = vertical ? &state->vertical : &state->horizontal;
        int total = *remainder + event->value;
        *remainder = total % 120;
        if (total / 120) {
            --out->count; /* Replace the final SYN, then append a new one. */
            uurb_emit(out, EV_REL, vertical ? REL_WHEEL : REL_HWHEEL, total / 120);
            uurb_emit(out, EV_SYN, SYN_REPORT, 0);
        }
        break;
    }
}
#endif
