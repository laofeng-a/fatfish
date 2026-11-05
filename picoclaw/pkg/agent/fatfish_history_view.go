package agent

import (
	"crypto/sha256"
	"fmt"
	"strings"
)

const fatfishEchoedHistoryNote = "思考：reasoning_content保留当时真实推断，完整记录按轮号可查。"

// Confirmed template echoes from the faulty history view. Match complete
// delivered text, not a blanket prefix rule: legitimate scripts stay verbatim.
var fatfishHistoryEchoes = map[string]bool{
	"96ef1c4b64576d219fc707ec3124ff3f64fe3b99e9c560018862d806f9a438d4": true,
	"599180cae8de664f1cf386fa18d2a87f43fedc5a0952230aea5c66ea1361a555": true,
	"5758a1c424ca76eed0fe94eee75d8e8c952ab57cf1afea10649d4811104c9405": true,
	"ce7c8cd7fc06ba9cebf96c7ecce9ad3283592155810bdd151a5692336f5fa304": true,
	"3c7f216cb91ac405918b39000edae289707c4359cddfb69260d0a2940441d700": true,
	"37c3ada75c8124f562f4c635e484482cb3b29b59090a2af5eb76f7c1c283e390": true,
	"76a64df057a1c2300fdc76eceb87c95b2fd2fb946e8e76cad473da8e8c46c89b": true,
	"294d9d6a6ecc5e32389726e5bc7904b5320fdc6dce54fd6298a6bc996b76c78f": true,
	"22c7660e6151112adc2f3aa5943807a68b6b74dc6a686d17cbb113ed5bd82573": true,
}

func fatfishDeliveredView(text string) string {
	if !fatfishHistoryEchoes[fmt.Sprintf("%x", sha256.Sum256([]byte(text)))] {
		return text
	}
	text = strings.TrimPrefix(text, "大肥鱼：")
	var lines []string
	for _, line := range strings.Split(text, "\n") {
		if line != fatfishEchoedHistoryNote {
			lines = append(lines, line)
		}
	}
	return strings.TrimRight(strings.Join(lines, "\n"), "\n")
}
