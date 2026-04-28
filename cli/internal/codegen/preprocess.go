//go:build codegen

// Command preprocess downgrades the Hail OpenAPI 3.1 spec into a 3.0-compatible
// form that oapi-codegen v2 can consume.
//
// FastAPI emits nullable fields with the OpenAPI 3.1 idiom
//
//	anyOf:
//	  - type: string
//	  - type: "null"
//
// oapi-codegen v2 (https://github.com/oapi-codegen/oapi-codegen/issues/373)
// does not yet support 3.1 and so cannot parse this. We rewrite to 3.0's
// `nullable: true` form. Two cases:
//
//  1. primitive: anyOf=[<inline schema>, null] -> inline + nullable: true
//  2. $ref:      anyOf=[{$ref: ...}, null]     -> allOf: [{$ref: ...}], nullable: true
//     ($ref siblings aren't valid in 3.0; allOf is the canonical wrap.)
//
// Run via `make codegen` — the output is fed to oapi-codegen, then discarded.
// The committed `openapi/openapi.yaml` remains the source of truth.
package main

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: preprocess <input.yaml> <output.yaml>")
		os.Exit(2)
	}
	in, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "read:", err)
		os.Exit(1)
	}
	var root yaml.Node
	if err := yaml.Unmarshal(in, &root); err != nil {
		fmt.Fprintln(os.Stderr, "parse:", err)
		os.Exit(1)
	}
	walk(&root)
	if root.Kind == yaml.DocumentNode && len(root.Content) > 0 {
		setScalar(root.Content[0], "openapi", "3.0.3")
	}
	out, err := yaml.Marshal(&root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "encode:", err)
		os.Exit(1)
	}
	if err := os.WriteFile(os.Args[2], out, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, "write:", err)
		os.Exit(1)
	}
}

func walk(n *yaml.Node) {
	if n == nil {
		return
	}
	if n.Kind == yaml.MappingNode {
		rewriteAnyOfNull(n)
		for i := 1; i < len(n.Content); i += 2 {
			walk(n.Content[i])
		}
		return
	}
	for _, c := range n.Content {
		walk(c)
	}
}

// rewriteAnyOfNull mutates m in place if m has exactly an `anyOf` of length 2
// where one element is `{type: "null"}`. Order of m's other keys is preserved
// (we walk m.Content linearly rather than going through a map).
func rewriteAnyOfNull(m *yaml.Node) {
	anyOfIdx := -1
	for i := 0; i < len(m.Content); i += 2 {
		if m.Content[i].Value == "anyOf" {
			anyOfIdx = i
			break
		}
	}
	if anyOfIdx == -1 {
		return
	}
	seq := m.Content[anyOfIdx+1]
	if seq.Kind != yaml.SequenceNode || len(seq.Content) != 2 {
		return
	}
	var nullPos, otherPos = -1, -1
	for i, item := range seq.Content {
		if isNullSchema(item) {
			nullPos = i
		} else {
			otherPos = i
		}
	}
	if nullPos == -1 || otherPos == -1 {
		return
	}
	other := seq.Content[otherPos]

	// Delete the anyOf key/value pair from m, preserving other entries' order.
	m.Content = append(m.Content[:anyOfIdx], m.Content[anyOfIdx+2:]...)

	if hasRef(other) {
		// $ref case: wrap in allOf so we can attach nullable.
		allOf := &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
		allOf.Content = []*yaml.Node{other}
		m.Content = append(m.Content,
			&yaml.Node{Kind: yaml.ScalarNode, Value: "allOf"},
			allOf,
		)
	} else if other.Kind == yaml.MappingNode {
		// Primitive case: inline the other schema's fields onto m.
		m.Content = append(m.Content, other.Content...)
	} else {
		// Should not occur for the Hail spec; bail out without setting nullable.
		return
	}
	setBool(m, "nullable", true)
}

func isNullSchema(n *yaml.Node) bool {
	if n.Kind != yaml.MappingNode {
		return false
	}
	for i := 0; i < len(n.Content); i += 2 {
		if n.Content[i].Value == "type" && n.Content[i+1].Value == "null" {
			return true
		}
	}
	return false
}

func hasRef(n *yaml.Node) bool {
	if n.Kind != yaml.MappingNode {
		return false
	}
	for i := 0; i < len(n.Content); i += 2 {
		if n.Content[i].Value == "$ref" {
			return true
		}
	}
	return false
}

func setScalar(m *yaml.Node, key, value string) {
	for i := 0; i < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = &yaml.Node{Kind: yaml.ScalarNode, Value: value}
			return
		}
	}
	m.Content = append(m.Content,
		&yaml.Node{Kind: yaml.ScalarNode, Value: key},
		&yaml.Node{Kind: yaml.ScalarNode, Value: value},
	)
}

func setBool(m *yaml.Node, key string, value bool) {
	v := "false"
	if value {
		v = "true"
	}
	for i := 0; i < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = &yaml.Node{Kind: yaml.ScalarNode, Value: v, Tag: "!!bool"}
			return
		}
	}
	m.Content = append(m.Content,
		&yaml.Node{Kind: yaml.ScalarNode, Value: key},
		&yaml.Node{Kind: yaml.ScalarNode, Value: v, Tag: "!!bool"},
	)
}
