package api

import (
	_ "embed"
	"net/http"

	"github.com/labstack/echo/v4"
)

// Generate swagger.json for the embedded API documentation.
//
//go:generate go run github.com/swaggo/swag/cmd/swag@v1.16.4 init -g main.go --dir ../../cmd/metald,.,../vm --parseInternal --outputTypes json -o .
//go:embed swagger.json
var specification []byte

// docsPage renders the embedded specification with a browser API reference.
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

// showDocumentation serves the API reference page.
func (s *Server) showDocumentation(c echo.Context) error {
	return c.HTML(http.StatusOK, docsPage)
}

// getOpenAPISpecification serves the embedded OpenAPI document.
func (s *Server) getOpenAPISpecification(c echo.Context) error {
	return c.JSONBlob(http.StatusOK, specification)
}
