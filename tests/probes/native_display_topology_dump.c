/* Print the topology the Win32 display shim parses from a display endpoint. */
#include "native_display_api.h"
#include <stdio.h>
int main(int argc, char **argv)
{
    static struct uurb_display_topology topology;
    if (argc != 2) return 2;
    int status = uurb_native_display_topology(argv[1], &topology, sizeof(topology));
    if (status) { printf("status %d\n", status); return 1; }
    for (unsigned i = 0; i < topology.monitor_count; ++i) {
        const struct uurb_display_monitor *m = &topology.monitors[i];
        printf("%s %d %d %u %u %u %u %u\n", m->gdi_name, (int)m->x, (int)m->y, m->width, m->height,
               m->refresh_millihz, m->scale_milli, m->flags & UURB_DISPLAY_MONITOR_PRIMARY);
    }
    return 0;
}
