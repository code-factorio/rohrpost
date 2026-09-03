//! The `rp` executable: parse argv, dispatch, map errors to exit codes.

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    std::process::exit(rohrpost::cli::main(&args));
}
