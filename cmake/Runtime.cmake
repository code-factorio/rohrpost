# Runtime-library linkage for release binaries.
#
# A release `rp` must run on a machine without the build toolchain: on Linux the
# C++ runtime is linked statically (glibc stays dynamic, it is everywhere), on
# Windows the CRT is static so no vcruntime redistributable is needed, and on
# macOS the system libc++ is always present so nothing changes.
function(rp_apply_runtime target)
  if(NOT RP_STATIC_RUNTIME)
    return()
  endif()
  if(MSVC)
    set_property(TARGET ${target} PROPERTY
      MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(${target} PRIVATE -static-libstdc++ -static-libgcc)
  endif()
endfunction()
