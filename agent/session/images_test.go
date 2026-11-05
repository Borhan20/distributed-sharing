package session

import (
	"fmt"
	"strings"
	"testing"
)

func TestRememberImageKeepsRecentDigests(t *testing.T) {
	a := &Agent{}
	d := func(i int) string { return fmt.Sprintf("sha256:%064x", i) }
	a.rememberImage("registry.example.org/lab/app:1@" + d(1))
	a.rememberImage("not-pinned:latest") // no digest: ignored
	a.rememberImage("x@" + d(2))
	a.rememberImage("y@" + d(1)) // seen again: moves to the end
	if got := strings.Join(a.recentImages, ","); got != d(2)+","+d(1) {
		t.Fatalf("got %s", got)
	}
	for i := 0; i < maxRecentImages+10; i++ {
		a.rememberImage("z@" + d(100+i))
	}
	if len(a.recentImages) != maxRecentImages || a.recentImages[len(a.recentImages)-1] != d(100+maxRecentImages+9) {
		t.Fatalf("kept %d, last %s", len(a.recentImages), a.recentImages[len(a.recentImages)-1])
	}
}
