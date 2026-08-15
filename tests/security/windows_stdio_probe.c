/*
 * Acceptance-only Windows stdio probe.
 *
 * This binary is compiled on the trusted Windows runner and copied into a
 * disposable workspace.  It is deliberately not part of the Neuro Code
 * package or runtime.  All child I/O uses ReadFile/WriteFile so the probe
 * cannot introduce CRT text-mode translation.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PROTOCOL_INPUT (4u * 1024u * 1024u)
#define READ_CHUNK 65536u
#define PAYLOAD_SPECIAL_LENGTH 13u

static const unsigned char payload_special[PAYLOAD_SPECIAL_LENGTH] = {
    0x00, 0x0D, 0x0A, 0x0D, 0x0A, 0xE2, 0x82, 0xAC,
    0xF0, 0x9F, 0x98, 0x80, 0xFF,
};

static unsigned char payload_byte(size_t index, unsigned int variant) {
    if (index < PAYLOAD_SPECIAL_LENGTH) {
        return payload_special[index];
    }
    return (unsigned char)((index * 37u + variant * 53u + 17u) & 0xFFu);
}

static int write_all(HANDLE handle, const unsigned char *data, size_t length) {
    while (length != 0u) {
        DWORD chunk = length > READ_CHUNK ? READ_CHUNK : (DWORD)length;
        DWORD written = 0u;
        if (!WriteFile(handle, data, chunk, &written, NULL) || written == 0u) {
            return 1;
        }
        data += written;
        length -= written;
    }
    return 0;
}

static int write_payload(HANDLE handle, size_t length, unsigned int variant) {
    unsigned char chunk[8192];
    size_t offset = 0u;
    while (offset < length) {
        size_t remaining = length - offset;
        DWORD count = remaining > sizeof(chunk) ? (DWORD)sizeof(chunk) : (DWORD)remaining;
        DWORD index = 0u;
        while (index < count) {
            chunk[index] = payload_byte(offset + index, variant);
            ++index;
        }
        if (write_all(handle, chunk, count) != 0) {
            return 1;
        }
        offset += count;
    }
    return 0;
}

static int write_capture(HANDLE stdout_handle, HANDLE stderr_handle) {
    static const unsigned char stdout_trailer[] =
        "G4_CAPTURE_STDOUT_TRAILER\x00\x0D\x0A\xFF";
    static const unsigned char stderr_trailer[] =
        "G4_CAPTURE_STDERR_TRAILER\x00\x0A\x0D\xFF";
    if (write_payload(stdout_handle, 131329u, 3u) != 0 ||
        write_all(stdout_handle, stdout_trailer, sizeof(stdout_trailer) - 1u) != 0) {
        return 1;
    }
    if (write_payload(stderr_handle, 131331u, 7u) != 0 ||
        write_all(stderr_handle, stderr_trailer, sizeof(stderr_trailer) - 1u) != 0) {
        return 1;
    }
    return 0;
}

static int write_merged(HANDLE merged_handle) {
    static const size_t lengths[] = {32771u, 32773u, 32779u, 32783u};
    static const unsigned int variants[] = {10u, 11u, 12u, 13u};
    static const unsigned char trailers[][16] = {
        "G4_MERGED_A\x00\x0D\x0A",
        "G4_MERGED_B\x00\x0A\x0D",
        "G4_MERGED_C\xFF\x0D\x0A",
        "G4_MERGED_D\xE2\x82\xAC\x0A",
    };
    static const size_t trailer_lengths[] = {14u, 14u, 14u, 15u};
    size_t index;
    for (index = 0u; index < 4u; ++index) {
        if (write_payload(merged_handle, lengths[index], variants[index]) != 0 ||
            write_all(merged_handle, trailers[index], trailer_lengths[index]) != 0) {
            return 1;
        }
    }
    return 0;
}

static int read_all(HANDLE handle, unsigned char **buffer_out, size_t *length_out) {
    unsigned char *buffer = NULL;
    size_t capacity = 0u;
    size_t length = 0u;
    for (;;) {
        DWORD count = 0u;
        unsigned char chunk[READ_CHUNK];
        BOOL ok = ReadFile(handle, chunk, sizeof(chunk), &count, NULL);
        if (!ok) {
            DWORD error = GetLastError();
            if (error == ERROR_BROKEN_PIPE) {
                count = 0u;
            } else {
                free(buffer);
                return 1;
            }
        }
        if (count == 0u) {
            break;
        }
        if (length > MAX_PROTOCOL_INPUT - (size_t)count) {
            free(buffer);
            return 1;
        }
        if (length + (size_t)count > capacity) {
            size_t next = capacity == 0u ? READ_CHUNK : capacity * 2u;
            while (next < length + (size_t)count) {
                next *= 2u;
            }
            if (next > MAX_PROTOCOL_INPUT) {
                next = MAX_PROTOCOL_INPUT;
            }
            unsigned char *grown = (unsigned char *)realloc(buffer, next);
            if (grown == NULL) {
                free(buffer);
                return 1;
            }
            buffer = grown;
            capacity = next;
        }
        memcpy(buffer + length, chunk, count);
        length += (size_t)count;
    }
    *buffer_out = buffer;
    *length_out = length;
    return 0;
}

static int validate_protocol(const unsigned char *data, size_t length) {
    size_t offset = 0u;
    unsigned int frames = 0u;
    while (offset < length) {
        uint32_t payload_length;
        if (length - offset < sizeof(uint32_t)) {
            return 1;
        }
        payload_length = ((uint32_t)data[offset]) |
            ((uint32_t)data[offset + 1u] << 8u) |
            ((uint32_t)data[offset + 2u] << 16u) |
            ((uint32_t)data[offset + 3u] << 24u);
        offset += sizeof(uint32_t);
        if (payload_length > MAX_PROTOCOL_INPUT ||
            (size_t)payload_length > length - offset) {
            return 1;
        }
        offset += (size_t)payload_length;
        ++frames;
        if (frames > 16u) {
            return 1;
        }
    }
    return frames == 0u ? 1 : 0;
}

static int run_protocol(HANDLE stdin_handle, HANDLE stdout_handle, HANDLE stderr_handle) {
    static const unsigned char diagnostic[] = "G4_PROTOCOL_DIAGNOSTIC\x00\x0D\x0A";
    unsigned char *input = NULL;
    size_t length = 0u;
    int result = read_all(stdin_handle, &input, &length);
    if (result == 0 && validate_protocol(input, length) != 0) {
        result = 1;
    }
    if (result == 0 && write_all(stdout_handle, input, length) != 0) {
        result = 1;
    }
    if (result == 0 && write_all(stderr_handle, diagnostic, sizeof(diagnostic) - 1u) != 0) {
        result = 1;
    }
    free(input);
    return result;
}

static int run_nonzero(HANDLE stdout_handle, HANDLE stderr_handle) {
    static const unsigned char stdout_value[] = "G4_NONZERO_STDOUT\x00\x0D\x0A\xFF";
    static const unsigned char stderr_value[] = "G4_NONZERO_STDERR\x00\x0A\x0D\xFF";
    if (write_all(stdout_handle, stdout_value, sizeof(stdout_value) - 1u) != 0 ||
        write_all(stderr_handle, stderr_value, sizeof(stderr_value) - 1u) != 0) {
        return 1;
    }
    return 7;
}

int main(int argc, char **argv) {
    HANDLE stdin_handle = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE stdout_handle = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE stderr_handle = GetStdHandle(STD_ERROR_HANDLE);
    if (stdin_handle == NULL || stdin_handle == INVALID_HANDLE_VALUE ||
        stdout_handle == NULL || stdout_handle == INVALID_HANDLE_VALUE ||
        stderr_handle == NULL || stderr_handle == INVALID_HANDLE_VALUE || argc < 2) {
        return 2;
    }
    if (strcmp(argv[1], "capture") == 0) {
        return write_capture(stdout_handle, stderr_handle);
    }
    if (strcmp(argv[1], "merged") == 0) {
        return write_merged(stdout_handle);
    }
    if (strcmp(argv[1], "protocol") == 0) {
        return run_protocol(stdin_handle, stdout_handle, stderr_handle);
    }
    if (strcmp(argv[1], "nonzero") == 0) {
        return run_nonzero(stdout_handle, stderr_handle);
    }
    return 2;
}
