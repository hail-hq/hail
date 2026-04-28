//go:build tools

// Package tools pins build-time tooling so `go mod tidy` keeps it in go.sum.
// This file is never built into the binary.
package tools

import (
	_ "github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen"
)
