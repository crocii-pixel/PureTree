#pragma once
#include "CPureTree.h"
#include "exTimer.h"
#include <memory>

namespace nsTree {

/**
 * @class CTimerTree
 * @brief An extension of CPureTree specifically designed to manage exTimer instances.
 * exTimer 인스턴스를 관리하기 위해 특화된 CPureTree 확장 클래스입니다.
 */
class CTimerTree : public CPureTree {
public:
    using TimerPtr = std::shared_ptr<exTimer>;

    /**
     * @brief Adds a node that contains an exTimer.
     * exTimer를 포함하는 노드를 추가합니다.
     * @param type Node classification / 노드 분류
     * @param key Unique identifier / 고유 식별자
     * @param intervalMs Timer interval / 타이머 간격
     * @param task Function to execute / 실행할 작업
     * @return Handle to the created node / 생성된 노드 핸들
     */
    hTree addTimerNode(int type, const std::string& key, uint32_t intervalMs, std::function<void()> task) {
        // exTimer is non-copyable, so we wrap it in a shared_ptr to store in AnyValue.
        // exTimer는 복사가 불가능하므로, AnyValue에 담기 위해 shared_ptr로 래핑합니다.
        auto timer = std::make_shared<exTimer>(std::move(task), intervalMs);
        return addNode(type, key, AnyValue(timer));
    }

    /**
     * @brief Safely retrieves the exTimer associated with a node handle.
     * 노드 핸들에 연결된 exTimer를 안전하게 가져옵니다.
     */
    TimerPtr getTimer(hTree h) {
        Node* node = getNode(h);
        if (node && node->value.has_value()) {
            try {
                // Return the shared_ptr from AnyValue
                return node->value.as<TimerPtr>();
            } catch (...) {
                return nullptr;
            }
        }
        return nullptr;
    }

    /**
     * @brief Hook for node removal to ensure the timer is stopped.
     * 노드 제거 시 타이머가 확실히 정지되도록 가상 함수를 재정의합니다.
     */
    void onNodeRemoving(hTree nodeId) override {
        TimerPtr timer = getTimer(nodeId);
        if (timer) {
            timer->stop();
            std::cout << "[TimerTree] Stopped timer for node: " << nodeId << "\n";
        }
    }
};

} // namespace nsTree