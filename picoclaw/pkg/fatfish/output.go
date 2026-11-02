package fatfish

import (
	"regexp"
	"strings"
)

var internalDeliveryHeader = regexp.MustCompile(`(?im)^\s*\[?Output history:\s*delivery facts\b`)
var confirmedDeliveryLine = regexp.MustCompile(`(?m)^\s*Confirmed sent (?:text|media|attachment):`)
var rawFailureHeader = regexp.MustCompile(`(?im)^\s*(?:Error processing message:|LLM call failed(?: after retries)?:|API request failed:|Traceback \(most recent call last\):)`)

// IsInternalDelivery identifies runtime records, including a model's echoed copy.
func IsInternalDelivery(content string) bool {
	return internalDeliveryHeader.MatchString(content) ||
		(strings.Contains(content, "[Delivery receipt:") && confirmedDeliveryLine.MatchString(content))
}

func SuppressOutput(content string, background bool) bool {
	text := strings.TrimSpace(content)
	return IsInternalDelivery(content) || rawFailureHeader.MatchString(content) || text == "HEARTBEAT_OK" || text == "NO_REPLY" || text == "[NO_REPLY]" ||
		(background && strings.Contains(content, "HEARTBEAT_OK"))
}
