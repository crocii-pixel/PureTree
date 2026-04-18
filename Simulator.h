#pragma once
#include "CPureTree.h" 
#include <unordered_map>
#include <queue>
#include <functional>
#include <iostream>
#include <cmath>
#include <iomanip>

namespace nsSim {

/**
 * @struct State
 * @brief Volatile simulation state managed separately from the tree core.
 * 트리의 노드에 직접 저장하지 않고, 시뮬레이터가 별도로 관리하는 휘발성 상태입니다.
 */
struct State {
    float energy = 0.0f;       // Accumulated energy (e.g., bug probability) / 누적된 활성화 에너지 (버그 가능성 등)
    float threshold = 1.0f;    // Activation threshold / 발동 임계점
    bool activated = false;    // Activation flag / 임계점 돌파 여부
};

/**
 * @brief Map to store state for each node handle.
 * 각 노드 핸들별 상태를 저장하는 맵입니다.
 */
using StateMap = std::unordered_map<nsTree::hTree, State>;

/**
 * @brief Strategy for energy propagation.
 * 에너지 전파 공식 전략입니다.
 */
using SpreadRule = std::function<float(float currentEnergy, const nsTree::Metrics& metrics)>;

/**
 * @struct Rules
 * @brief Pre-defined propagation strategies.
 * 미리 정의된 확산 전략 모음입니다.
 */
struct Rules {
    // Linear impact assessment / 선형 영향도 평가
    static float Linear(float energy, const nsTree::Metrics& m) {
        return (energy * m.force) / std::max(0.1f, m.distance);
    }
    
    // Exponential decay (Rumor or heat spread) / 지수 감쇠 (소문이나 열 확산)
    static float Decay(float energy, const nsTree::Metrics& m) {
        return energy * m.force * std::exp(-m.distance);
    }
    
    // Logic gate trigger / 논리 게이트 트리거
    static float Gate(float energy, const nsTree::Metrics& m) {
        return (energy >= 1.0f && m.force > 0.5f) ? 1.0f : 0.0f;
    }
};

/**
 * @class Simulator
 * @brief Universal Graph Simulator for energy propagation.
 * 범용 그래프 시뮬레이터입니다.
 */
class Simulator {
public:
    /**
     * @brief Executes the simulation starting from the epicenter.
     * 특정 지점에서 시작하여 에너지를 확산시킵니다.
     */
    static StateMap run(nsTree::CPureTree& tree, nsTree::hTree startNode, float initialEnergy, SpreadRule rule = Rules::Linear) {
        StateMap states;
        if (!tree.getNode(startNode)) return states;

        // Epsilon defines the "Noise Floor". Energy below this value will not propagate.
        // 에너지가 이 값보다 낮으면 더 이상 확산되지 않도록 차단하는 최소 임계치입니다.
        const float epsilon = 0.001f;

        std::queue<nsTree::hTree> q;
        
        // Initial setup for the epicenter
        // 발원지 초기 설정
        q.push(startNode);
        states[startNode].energy = initialEnergy;
        states[startNode].activated = (std::abs(initialEnergy) >= states[startNode].threshold);

        auto* originNode = tree.getNode(startNode);
        
        std::cout << "\n===========================================\n";
        std::cout << " [Simulation Start] Epicenter: '" << originNode->key << "' (Impact: " << initialEnergy << ")\n";
        std::cout << "===========================================\n";

        while (!q.empty()) {
            auto curId = q.front();
            q.pop();

            const nsTree::Node* curNode = tree.getNode(curId);
            if (!curNode) continue;

            float curEnergy = states[curId].energy;
            
            // Skip propagation if the current node's energy is negligible
            // 현재 노드의 에너지가 너무 낮으면 확산을 중단합니다.
            if (std::abs(curEnergy) < epsilon) continue;

            for (auto linkId : curNode->outs) {
                const nsTree::Link* link = tree.getLink(linkId);
                if (!link || !tree.getNode(link->to)) continue;

                // 1. Calculate transferred energy with polarity
                // 1. 전달 에너지 계산 및 극성 반영
                float transfer = rule(curEnergy, link->metrics) * link->metrics.polarity;
                
                // 2. Ignore noise (insignificant energy transfers)
                // 2. 무의미한 수준의 미세한 에너지는 무시합니다.
                if (std::abs(transfer) < epsilon) continue;

                // 3. Accumulate energy on target
                // 3. 타겟 노드에 에너지 누적
                auto& targetState = states[link->to];
                targetState.energy += transfer;

                // 4. Chain reaction trigger
                // 4. 연쇄 발동 처리
                if (std::abs(targetState.energy) >= targetState.threshold && !targetState.activated) {
                    targetState.activated = true;
                    q.push(link->to); 
                    
                    const nsTree::Node* targetNode = tree.getNode(link->to);
                    std::cout << "  >> [Chain Reaction] Node: '" << targetNode->key 
                              << "' | Energy: " << std::fixed << std::setprecision(2) << targetState.energy << "\n";
                }
            }
        }
        
        std::cout << "-------------------------------------------\n";
        std::cout << " [Simulation End] Analysis Complete\n";
        std::cout << "===========================================\n";
        
        return states; 
    }
};

} // namespace nsSim