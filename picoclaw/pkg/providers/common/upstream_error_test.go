package common

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestGatewayFailureMetadataSurvivesLargeBody(t *testing.T) {
	body := "{\"error\":{\"upstream_status\":403,\"upstream_code\":\"content_filter\",\"upstream_body\":\"" + strings.Repeat("x", 1000) + "\",\"message\":\"blocked\"}}"
	response := &http.Response{StatusCode: 403, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(body))}
	var err *HTTPError
	if !errors.As(HandleErrorResponse(response, "http://gateway"), &err) || err.UpstreamStatus != 403 || err.UpstreamCode != "content_filter" || len(err.UpstreamBody) != 1000 {
		t.Fatal("lost gateway classification/response summary")
	}
}
