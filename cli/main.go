package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "hail: M1 in progress. See docs/architecture.md.")
	os.Exit(1)
}
