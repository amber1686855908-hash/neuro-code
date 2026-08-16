#include <windows.h>

int main(void) {
    const char marker[] = "W5_GATE111_PRAW_ENTRY\n";
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output == NULL || output == INVALID_HANDLE_VALUE ||
        !WriteFile(output, marker, (DWORD)(sizeof(marker) - 1), &written, NULL) ||
        written != (DWORD)(sizeof(marker) - 1)) {
        return 91;
    }
    return 0;
}
