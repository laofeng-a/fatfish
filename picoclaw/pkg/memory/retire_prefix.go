package memory

import (
	"context"
	"fmt"
	"sort"
	"time"
)

func (s *JSONLStore) RetirePrefix(_ context.Context, key string, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	lock := s.sessionLock(key)
	lock.Lock()
	defer lock.Unlock()
	meta, err := s.readMeta(key)
	if err != nil {
		return err
	}
	messages, err := readMessages(s.jsonlPath(key), meta.Skip)
	if err != nil {
		return err
	}
	if len(messages) < len(ids) {
		return fmt.Errorf("history prefix changed")
	}
	for i, id := range ids {
		if id == "" || messages[i].HistoryID != id {
			return fmt.Errorf("history prefix changed")
		}
	}
	rawCount, retained, err := scanRetainedMessageLines(s.jsonlPath(key))
	if err != nil {
		return err
	}
	start := sort.Search(len(retained), func(i int) bool { return retained[i] > meta.Skip })
	if start+len(ids) > len(retained) {
		return fmt.Errorf("history source changed")
	}
	meta.Count = rawCount
	meta.Skip = retained[start+len(ids)-1]
	meta.UpdatedAt = time.Now()
	return s.writeMeta(key, meta)
}
