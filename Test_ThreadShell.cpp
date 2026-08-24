#include <iostream>
#include <chrono>
#include "CPureTree.h"
#include "ThreadShell.h"

/**
 * @brief Simulation of an Active Processing Graph.
 * 활성 처리 그래프 시뮬레이션입니다.
 */
int main() {
    nsTree::CPureTree tree;

    std::cout << "--- 1. Creating Active Worker Nodes ---\n";

    // Create a worker node that holds a ThreadShell
    // ThreadShell을 보유한 워커 노드를 생성합니다.
    nsTree::hTree workerH = tree.addNode(10, "Worker_01", CThreadShell());

    // Access the shell from the node
    // 노드에서 쉘에 접근합니다.
    nsTree::Node* node = tree.getNode(workerH);
    CThreadShell& shell = node->value.get<CThreadShell>();

    // 2. Define a background task (e.g., monitoring or processing)
    // 2. 배경 작업 정의 (예: 모니터링 또는 처리)
    auto task = [](std::atomic<bool>& stop) {
        int count = 0;
        while (!stop && count < 5) {
            std::cout << "  [Worker] Processing data batch " << ++count << "...\n";
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
        }
    };

    // 3. Launch the worker via the tree node
    // 3. 트리 노드를 통해 워커를 실행합니다.
    std::cout << "--- 2. Launching Background Task via Tree Node ---\n";
    shell.launch(node->key, task);

    // Main thread does other things
    // 메인 쓰레드는 다른 작업을 수행합니다.
    std::cout << "--- 3. Main Thread is Free to Do Other Work ---\n";
    for(int i=0; i<3; ++i) {
        std::cout << "  [Main] Doing heavy UI work...\n";
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
    }

    // 4. Cleanup
    // 4. 정리 작업
    std::cout << "--- 4. Shutting Down ---\n";
    if (shell.isRunning()) {
        shell.stop();
    }

    std::cout << "All active nodes processed.\n";
    return 0;
}