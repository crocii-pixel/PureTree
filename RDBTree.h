#pragma once

#include "CPureTree.h"
#include <unordered_map>
#include <unordered_set>
#include <string>
#include <mutex>
#include <shared_mutex>
#include <iostream>

namespace nsTree {

/**
 * @class CRDBTree
 * @brief Extension of CPureTree serving as the Hybrid In-Memory Business Tier.
 * CPureTree를 상속받아 더티 플래그(Dirty Flag) 및 Handle ↔ PK 매핑을 지원하는 비즈니스 계층 트리.
 */
class CRDBTree : public CPureTree {
public:
    CRDBTree(const TreeConfig& config = TreeConfig());
    virtual ~CRDBTree() = default;

    // ==========================================
    // 1. RDB Primary Key Mapping
    // ==========================================

    /**
     * @brief Adds a node while mapping it to a global DB Primary Key.
     * DB의 테이블명과 기본키(PK)를 조합하여 글로벌 고유 키로 매핑하며 노드를 추가합니다.
     */
    hTree addNodeWithPK(const std::string& tableName, const std::string& pk, int type, AnyValue val = AnyValue());

    /**
     * @brief Retrieves a node handle (hTree) given its original DB identity.
     */
    hTree getNodeByPK(const std::string& tableName, const std::string& pk) const;

    /**
     * @brief Retrieves the formatted global PK from an internal node handle.
     */
    std::string getFormattedPKByNode(hTree h) const;

    // ==========================================
    // 2. Entity State Tracking (Dirty Flag)
    // ==========================================

    /**
     * @brief Sets the value of a node and marks it as dirty for lazy db synchronization.
     * 노드 값을 수정하고 DB 동기화를 위해 더티 세트에 추가합니다.
     */
    virtual void setNodeValue(hTree nodeId, AnyValue newVal) override;

    /**
     * @brief Hook called when a node is removed, used to clear its PK mappings and dirty state.
     */
    virtual void onNodeRemoving(hTree nodeId) override;

    /**
     * @brief Flushes and clears the dirty states. (Placeholder for Lazy DB Write)
     */
    void flushDirtyNodes();

private:
    /**
     * @brief Formats the specific table PK to a generalized global unique string.
     */
    inline std::string formatPK(const std::string& tableName, const std::string& pk) const {
        return "db_" + tableName + "_" + pk;
    }

    void markDirty(hTree h);

    mutable std::shared_mutex m_mapMutex;
    std::unordered_map<std::string, hTree> m_pkToHandle;
    std::unordered_map<hTree, std::string> m_handleToPk;

    mutable std::mutex m_dirtyMutex;
    std::unordered_set<hTree> m_dirtyNodes;
};

} // namespace nsTree
