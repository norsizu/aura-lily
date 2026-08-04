#include "music_library.h"

#include <dirent.h>
#include <strings.h>
#include <string.h>

static bool is_supported_music_ext(const char *name)
{
    const char *ext = strrchr(name, '.');
    if (!ext) {
        return false;
    }
    return strcasecmp(ext, ".mp3") == 0 ||
           strcasecmp(ext, ".wav") == 0;
}

bool music_library_has_supported_files(const char *root_dir)
{
    if (!root_dir || root_dir[0] == '\0') {
        return false;
    }

    DIR *dir = opendir(root_dir);
    if (!dir) {
        return false;
    }

    bool found = false;
    struct dirent *entry = NULL;
    while ((entry = readdir(dir)) != NULL) {
        if (entry->d_name[0] == '.') {
            continue;
        }
        if (is_supported_music_ext(entry->d_name)) {
            found = true;
            break;
        }
    }
    closedir(dir);
    return found;
}
