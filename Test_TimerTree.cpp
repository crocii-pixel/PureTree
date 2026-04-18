#include <iostream>
#include "TimerTree.h"

int main() {
    nsTree::CTimerTree tree;

    std::cout << "--- [PureTimer System Start] ---\n";

    // 1. Create a periodic logic pulse node
    auto hPulse = tree.addTimerNode(100, "LogicPulse", 500, []() {
        static int count = 0;
        std::cout << "  [Heartbeat] Pulse #" << ++count << " processed.\n";
    });

    // 2. Control the timer
    if (auto timer = tree.getTimer(hPulse)) {
        std::cout << "--- Starting Background Workers ---\n";
        timer->start();
    }

    // Main loop simulation (3 seconds)
    for (int i = 0; i < 3; ++i) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        std::cout << "  [Main] App state is healthy...\n";
    }

    // 3. Automated cleanup of background threads
    std::cout << "--- Stopping and Removing Nodes ---\n";
    tree.removeNode(hPulse);

    std::cout << "--- [PureTimer System Shutdown Cleanly] ---\n";
    return 0;
}