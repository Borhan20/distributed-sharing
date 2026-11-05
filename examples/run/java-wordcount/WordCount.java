// Count the words of /app/text.txt and write the most common ones to /out.
// Each part takes every n-th line (POOL_TASK_INDEX of POOL_TASK_COUNT); one part does all of them.
// The JVM sizes its heap from the container's memory limit (see the Dockerfile), so no -Xmx is needed.
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

public class WordCount {
    public static void main(String[] args) throws Exception {
        int part = Integer.parseInt(System.getenv().getOrDefault("POOL_TASK_INDEX", "0"));
        int parts = Integer.parseInt(System.getenv().getOrDefault("POOL_TASK_COUNT", "1"));
        int top = args.length > 0 ? Integer.parseInt(args[0]) : 10;
        System.out.printf("JVM max heap: %d MiB%n", Runtime.getRuntime().maxMemory() / (1024 * 1024));

        List<String> lines = Files.readAllLines(Path.of("/app/text.txt"));
        Map<String, Integer> counts = new HashMap<>();
        int words = 0;
        for (int i = part; i < lines.size(); i += parts) {
            for (String w : lines.get(i).toLowerCase(Locale.ROOT).split("[^a-z']+")) {
                if (w.isEmpty()) continue;
                counts.merge(w, 1, Integer::sum);
                words++;
            }
        }

        List<Map.Entry<String, Integer>> sorted = counts.entrySet().stream()
            .sorted(Map.Entry.<String, Integer>comparingByValue().reversed().thenComparing(Map.Entry.comparingByKey()))
            .limit(top).toList();
        StringBuilder csv = new StringBuilder("word,count\n");
        for (Map.Entry<String, Integer> e : sorted) {
            csv.append(e.getKey()).append(',').append(e.getValue()).append('\n');
            System.out.printf("%-12s %d%n", e.getKey(), e.getValue());
        }
        Files.writeString(Path.of("/out/top_words.csv"), csv);
        Files.writeString(Path.of("/out/result.json"), String.format(
            "{\"part\": %d, \"words\": %d, \"distinct_words\": %d}", part, words, counts.size()));
        System.out.printf("done: %d words, %d distinct%n", words, counts.size());
    }
}
