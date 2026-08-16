#include <windows.h>

void gate110_raw_entry(void) {
    const char marker[] = "W5_GATE110_PRAW_ENTRY\n";
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, marker, (DWORD)(sizeof(marker) - 1), &written, NULL);
    }
    ExitProcess(written == (DWORD)(sizeof(marker) - 1) ? 0 : 91);
}
