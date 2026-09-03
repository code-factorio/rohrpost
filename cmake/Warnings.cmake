# Compiler warning set shared by every target. Kept in one place so the
# release and test builds cannot drift apart.
function(rp_apply_warnings target)
  if(MSVC)
    target_compile_options(${target} PRIVATE /W4 /permissive- /utf-8 /Zc:__cplusplus /Zc:preprocessor /EHsc)
    if(RP_WERROR)
      target_compile_options(${target} PRIVATE /WX)
    endif()
  else()
    target_compile_options(${target} PRIVATE
      -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
      -Wnon-virtual-dtor -Wold-style-cast -Wcast-align -Wunused
      -Woverloaded-virtual -Wdouble-promotion -Wformat=2
      -Wno-missing-field-initializers)
    if(RP_WERROR)
      target_compile_options(${target} PRIVATE -Werror)
    endif()
  endif()
endfunction()
