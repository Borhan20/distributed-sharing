# Bootstrap 95% confidence intervals for the mean and the median of made-up waiting times.
# Each part draws its own resamples with its own seed (POOL_TASK_INDEX of POOL_TASK_COUNT),
# so more parts give more resamples. Writes /out/result.json and /out/intervals.csv.
part <- as.integer(Sys.getenv("POOL_TASK_INDEX", "0"))
parts <- as.integer(Sys.getenv("POOL_TASK_COUNT", "1"))
args <- commandArgs(trailingOnly = TRUE)
resamples <- if (length(args) > 0) as.integer(args[1]) else 20000

set.seed(42)                               # the same data in every part
x <- rgamma(300, shape = 2, scale = 4)     # synthetic waiting times, in minutes
set.seed(1000 + part)                      # but different resamples
means <- numeric(resamples)
medians <- numeric(resamples)
for (i in seq_len(resamples)) {
  s <- sample(x, replace = TRUE)
  means[i] <- mean(s)
  medians[i] <- median(s)
}

ci <- function(v) quantile(v, c(0.025, 0.975), names = FALSE)
m <- ci(means)
d <- ci(medians)
cat(sprintf("mean   %.3f  95%% CI [%.3f, %.3f]\n", mean(x), m[1], m[2]))
cat(sprintf("median %.3f  95%% CI [%.3f, %.3f]\n", median(x), d[1], d[2]))

write.csv(data.frame(statistic = c("mean", "median"), estimate = c(mean(x), median(x)),
                     lower = c(m[1], d[1]), upper = c(m[2], d[2])),
          "/out/intervals.csv", row.names = FALSE)
writeLines(sprintf(paste0('{"part": %d, "resamples": %d, "mean": %.4f, "mean_lower": %.4f, ',
                          '"mean_upper": %.4f, "median": %.4f, "median_lower": %.4f, "median_upper": %.4f}'),
                   part, resamples, mean(x), m[1], m[2], median(x), d[1], d[2]),
           "/out/result.json")
cat("done:", resamples, "resamples\n")
