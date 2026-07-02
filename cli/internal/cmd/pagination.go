package cmd

import (
	"fmt"
	"io"
)

// cursorPage is one page of a cursor-paginated API response.
type cursorPage[T any] struct {
	items      []T
	nextCursor *string
}

// walkCursor drives the shared pagination shape of every list command.
//
// Single-page mode (all=false): one fetch; the caller gets the page's
// next_cursor back so it can print the "more: --cursor" hint. With
// all=true it follows next_cursor to exhaustion, warning once on stderr
// past 1000 items (stderr so `--all --json | jq` keeps working) and
// returns a nil cursor.
func walkCursor[T any](
	all bool,
	start string,
	stderr io.Writer,
	noun string,
	fetch func(cursor string) (cursorPage[T], error),
) ([]T, *string, error) {
	cursor := start
	warned := false
	var items []T
	for {
		page, err := fetch(cursor)
		if err != nil {
			return nil, nil, err
		}
		items = append(items, page.items...)
		if !all {
			return items, page.nextCursor, nil
		}
		if !warned && len(items) > 1000 {
			fmt.Fprintf(stderr, "warning: walked %d %s so far; ctrl-C to stop\n", len(items), noun)
			warned = true
		}
		if page.nextCursor == nil || *page.nextCursor == "" {
			return items, nil, nil
		}
		cursor = *page.nextCursor
	}
}
