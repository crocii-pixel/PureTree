#include "RDBTree.h"

namespace nsTree {

CRDBTree::CRDBTree(const TreeConfig& config) : CPureTree(config) {}

hTree CRDBTree::addNodeWithPK(const std::string& tableName, const std::string& pk, int type, AnyValue val) {
    if (tableName.empty() || pk.empty()) return 0;

    std::string globalKey = formatPK(tableName, pk);

    // Tree creation
    hTree newHandle = addNode(type, globalKey, std::move(val));
    if (newHandle == 0) return 0;

    // Register to mapper
    {
        std::unique_lock<std::shared_mutex> lock(m_mapMutex);
        m_pkToHandle[globalKey] = newHandle;
        m_handleToPk[newHandle] = globalKey;
    }

    // New nodes might need to be flushed to DB later
    markDirty(newHandle);

    return newHandle;
}

hTree CRDBTree::getNodeByPK(const std::string& tableName, const std::string& pk) const {
    std::string globalKey = formatPK(tableName, pk);
    std::shared_lock<std::shared_mutex> lock(m_mapMutex);
    auto it = m_pkToHandle.find(globalKey);
    if (it != m_pkToHandle.end()) {
        return it->second;
    }
    return 0;
}

std::string CRDBTree::getFormattedPKByNode(hTree h) const {
    std::shared_lock<std::shared_mutex> lock(m_mapMutex);
    auto it = m_handleToPk.find(h);
    if (it != m_handleToPk.end()) {
        return it->second;
    }
    return "";
}

void CRDBTree::setNodeValue(hTree nodeId, AnyValue newVal) {
    // Rely on base class implementation
    CPureTree::setNodeValue(nodeId, std::move(newVal));
    
    // Track modification
    markDirty(nodeId);
}

void CRDBTree::onNodeRemoving(hTree nodeId) {
    // Base class behavior
    CPureTree::onNodeRemoving(nodeId);

    // Remove from mapper
    {
        std::unique_lock<std::shared_mutex> lock(m_mapMutex);
        auto it = m_handleToPk.find(nodeId);
        if (it != m_handleToPk.end()) {
            m_pkToHandle.erase(it->second);
            m_handleToPk.erase(it);
        }
    }

    // Unmark dirty if deleted to avoid flushing dead nodes
    {
        std::lock_guard<std::mutex> lock(m_dirtyMutex);
        m_dirtyNodes.erase(nodeId);
    }
}

void CRDBTree::flushDirtyNodes() {
    std::unordered_set<hTree> snapshot;
    {
        std::lock_guard<std::mutex> lock(m_dirtyMutex);
        snapshot = std::move(m_dirtyNodes);
        m_dirtyNodes.clear(); // Reset immediately
    }

    // In a real application, iterate over snapshot and persist changes to the backing RDB
    // using CPublisher, DB abstractions, etc.
    if (!snapshot.empty()) {
        std::cout << "[CRDBTree] Flushed " << snapshot.size() << " dirty entities to RDB Skeleton.\n";
    }
}

void CRDBTree::markDirty(hTree h) {
    std::lock_guard<std::mutex> lock(m_dirtyMutex);
    m_dirtyNodes.insert(h);
}

} // namespace nsTree
