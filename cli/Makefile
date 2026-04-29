.PHONY: codegen build test vet fmt release-snapshot

# Regenerate the OpenAPI client from openapi/openapi.yaml.
#
# We preprocess the spec first because oapi-codegen v2 does not yet support
# OpenAPI 3.1's `anyOf: [<type>, "null"]` nullable idiom (issue #373); the
# preprocessor downgrades it to 3.0.3 with `nullable: true`. The committed
# openapi.yaml stays in 3.1 — it is the source of truth for every generator.
#
# The generated file is committed; CI does not run this target.
codegen:
	@TMP=$$(mktemp -t hail-spec.XXXXXX.yaml) && \
		go run -tags codegen ./internal/codegen "../openapi/openapi.yaml" "$$TMP" && \
		go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen \
			-package client \
			-generate types,client \
			-o internal/client/client.gen.go \
			"$$TMP" && \
		rm -f "$$TMP"
	gofmt -w internal/client

build:
	go build ./...

test:
	go test ./...

vet:
	go vet ./...

fmt:
	gofmt -w .

# Build a local GoReleaser snapshot — multi-arch tarballs into ./dist/ — without
# publishing to GitHub Releases or pushing the Homebrew formula. Use this to
# validate .goreleaser.yml after editing it. CI uses goreleaser-action instead.
#
# Requires goreleaser on PATH:
#     brew install goreleaser
# or:
#     go install github.com/goreleaser/goreleaser/v2@latest
release-snapshot:
	goreleaser release --snapshot --skip=publish --clean
