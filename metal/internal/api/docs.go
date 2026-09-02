package api

import (
	_ "embed"
	"net/http"

	"github.com/labstack/echo/v4"
)

// swagger.json is generated, not committed. Run `make openapi` after a clone,
// or `go generate ./...`.
//
//go:generate go run github.com/swaggo/swag/cmd/swag@v1.16.4 init -g main.go --dir ../../cmd/metald,. --parseInternal --outputTypes json -o .
//go:embed swagger.json
var specification []byte

const docsPage = `<!doctype html>
<html lang="en">
	<head>
		<title>Metal API</title>
		<meta charset="utf-8">
		<meta name="viewport" content="width=device-width, initial-scale=1">
	</head>
	<body>
		<div id="app"></div>
		<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
		<script>
			Scalar.createApiReference("#app", {
				url: "/docs/swagger.json",
				agent: {
					disabled: true,
				}
			});
		</script>
	</body>
</html>
`

// docs serves the page that displays the application programming interface specification.
func (s *Server) docs(c echo.Context) error {
	return c.HTML(http.StatusOK, docsPage)
}

// specificationJSON serves the embedded API specification.
func (s *Server) specificationJSON(c echo.Context) error {
	return c.JSONBlob(http.StatusOK, specification)
}
