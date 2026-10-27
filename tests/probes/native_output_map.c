#include "uu_native_input_translate.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>

/* The owner's desk: DP-2 3840x2160 at 140 % on the left, DP-1 2560x1440 at
 * 100 % on the right; KWin spans 2743 + 2560 by 1543 logical pixels. */
static const char *desk = "0,0,3840,2160,0,0,33897,65535;3845,0,2560,1440,33898,0,31637,61166";

static void moved(const struct uurb_output_map *map, unsigned flags, int x, int y, int ex, int ey)
{
    struct uurb_native_events out;
    uurb_x11_input_event in = {.type = UURB_X11_INPUT_MOUSE, .flags = flags, .x = x, .y = y};
    assert(uurb_native_translate_mapped(&in, map, &out));
    assert(out.events[0].code == ABS_X && out.events[1].code == ABS_Y);
    if (abs(out.events[0].value - ex) > 2 || abs(out.events[1].value - ey) > 2) {
        printf("flags %#x (%d,%d) -> (%d,%d), expected (%d,%d)\n", flags, x, y,
               out.events[0].value, out.events[1].value, ex, ey);
        fflush(stdout);
        abort();
    }
}

int main(void)
{
    struct uurb_output_map map;
    assert(uurb_output_map_parse(desk, &map) && map.count == 2);
    const unsigned absolute_move = 0x8001, virtual_desk = 0x4000;
    /* Virtual desktop is 6405x2160 Windows pixels. Centre of DP-2. */
    moved(&map, absolute_move | virtual_desk, 19646, 32768, 16948, 32767);
    /* Centre of DP-1 at Windows (5125,720). */
    moved(&map, absolute_move | virtual_desk, 52439, 21845, 49716, 30582);
    /* Top-left and bottom-right corners of DP-1. */
    moved(&map, absolute_move | virtual_desk, 39343, 0, 33898, 0);
    moved(&map, absolute_move | virtual_desk, 65535, 43690, 65534, 61165);
    /* Without VIRTUALDESK, SendInput addresses the primary monitor. */
    moved(&map, absolute_move, 32768, 32768, 16948, 32767);
    moved(&map, absolute_move, 65535, 65535, 33896, 65534);
    /* The 5 px layout gap and the area under DP-1 snap to the nearest monitor. */
    moved(&map, absolute_move | virtual_desk, 39300, 32768, 33896, 32767);
    moved(&map, absolute_move | virtual_desk, 39325, 32768, 33898, 45874);
    moved(&map, absolute_move | virtual_desk, 52439, 60000, 49716, 61165);
    /* Relative moves and button-only events are untouched. */
    struct uurb_native_events out;
    uurb_x11_input_event in = {.type = UURB_X11_INPUT_MOUSE, .flags = 1, .x = -7, .y = 9};
    assert(uurb_native_translate_mapped(&in, &map, &out) && out.events[0].type == EV_REL && out.events[0].value == -7);
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_MOUSE, .flags = 0xc002, .x = 30000, .y = 20000};
    assert(uurb_native_translate_mapped(&in, &map, &out) && out.events[0].type == EV_KEY);
    /* An empty map keeps the unmapped behaviour. */
    struct uurb_output_map none = {0};
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_MOUSE, .flags = 0xc001, .x = 1234, .y = 4321};
    assert(uurb_native_translate_mapped(&in, &none, &out) && out.events[0].value == 1234 && out.events[1].value == 4321);
    const char *invalid[] = {"", "1,2,3", "0,0,3840,2160,0,0,65535,65535;", "0,0,3840,2160,0,0,65535,65535x",
                             "0,0,3840,2160,2,0,65535,65535", "0,0,3840,2160,0,0,0,65535",
                             "0,0,4098,2160,0,0,65535,65535", "0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;"
                             "0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1;0,0,2,2,0,0,1,1"};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(*invalid); ++i)
        if (uurb_output_map_parse(invalid[i], &map)) { printf("accepted %s\n", invalid[i]); return 1; }
    puts("output map checks passed");
    return 0;
}
