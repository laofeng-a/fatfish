package memory

import (
	"errors"
	"fmt"
	"io"
	"os"
)

type jsonlAppendFile interface {
	Stat() (os.FileInfo, error)
	Write([]byte) (int, error)
	Sync() error
	Truncate(int64) error
	Name() string
}

// The caller holds the session lock. A failed append must leave no fragment
// onto which the next valid message can be concatenated after disk recovery.
func appendJSONLRecord(f jsonlAppendFile, line []byte) error {
	before, err := f.Stat()
	if err != nil {
		return fmt.Errorf("memory: stat before append: %w", err)
	}
	n, err := f.Write(line)
	if err == nil && n != len(line) {
		err = io.ErrShortWrite
	}
	if err == nil {
		err = f.Sync()
	}
	if err == nil {
		return nil
	}
	// Windows append-only handles cannot truncate; use a fresh write handle.
	rollback := os.Truncate(f.Name(), before.Size())
	if rollback == nil {
		rollback = f.Sync()
	}
	return fmt.Errorf("memory: append failed: %w", errors.Join(err, rollback))
}
