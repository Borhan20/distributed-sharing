// Count the primes below N on all the CPUs Go sees; the job's --cpus caps how much CPU time they get.
// Writes /out/result.json.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"runtime"
	"sync"
	"time"
)

func isPrime(n int) bool {
	if n < 2 {
		return false
	}
	for d := 2; d*d <= n; d++ {
		if n%d == 0 {
			return false
		}
	}
	return true
}

func main() {
	limit := flag.Int("below", 5_000_000, "count primes below this number")
	flag.Parse()
	start := time.Now()
	workers := runtime.GOMAXPROCS(0)
	counts := make([]int, workers)
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for n := w; n < *limit; n += workers {
				if isPrime(n) {
					counts[w]++
				}
			}
		}(w)
	}
	wg.Wait()
	total := 0
	for _, c := range counts {
		total += c
	}
	result := map[string]any{"below": *limit, "primes": total, "workers": workers,
		"seconds": time.Since(start).Seconds()}
	fmt.Printf("%d primes below %d (%d workers, %.2fs)\n", total, *limit, workers, time.Since(start).Seconds())
	out, _ := json.Marshal(result)
	if err := os.WriteFile("/out/result.json", out, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
