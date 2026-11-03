// A 2-D heat diffusion simulation. Writes the final temperature field to /out/heat.csv.
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    int size = 200, steps = 2000;
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string flag = argv[i];
        if (flag == "--size") size = std::atoi(argv[i + 1]);
        if (flag == "--steps") steps = std::atoi(argv[i + 1]);
    }
    std::vector<double> grid(size * size, 0.0), next(size * size, 0.0);
    for (int y = size / 3; y < 2 * size / 3; ++y)
        for (int x = size / 3; x < 2 * size / 3; ++x) grid[y * size + x] = 100.0;  // a hot square
    for (int s = 0; s < steps; ++s) {
        for (int y = 1; y < size - 1; ++y)
            for (int x = 1; x < size - 1; ++x) {
                int i = y * size + x;
                next[i] = grid[i] + 0.2 * (grid[i - 1] + grid[i + 1] + grid[i - size] + grid[i + size] - 4 * grid[i]);
            }
        grid.swap(next);
        if (s % 500 == 0) std::cout << "step " << s << " of " << steps << std::endl;
    }
    std::ofstream out("/out/heat.csv");
    if (!out) {
        std::cerr << "cannot write /out/heat.csv" << std::endl;
        return 1;
    }
    out << "x,y,temperature\n";
    for (int y = 0; y < size; y += 10)
        for (int x = 0; x < size; x += 10) out << x << "," << y << "," << grid[y * size + x] << "\n";
    std::cout << "done: " << size << "x" << size << " grid, " << steps << " steps" << std::endl;
    return 0;
}
