#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <mutex>
#include <shared_mutex>
#include <stdexcept>
#include <initializer_list>

namespace nsTree {

/**
 * @class TypeRegistry
 * @brief Manages mapping of semantic string types (e.g. "db_Customer") to internal integer identifiers.
 * 문자열 타입과 내부 정수 ID 간의 매핑을 관리하는 레지스트리.
 */
class TypeRegistry {
public:
    static TypeRegistry& instance() {
        static TypeRegistry s_instance;
        return s_instance;
    }

    /**
     * @brief Resolves a string type to an integer ID. Registers it if it doesn't exist.
     * 문자열 타입을 정수 ID로 변환. 존재하지 않는 경우 새로 등록합니다.
     * @param typeName type string (e.g. "db_User")
     * @return the unique integer ID
     */
    int resolve(const std::string& typeName) {
        if (typeName.empty()) return 0; // 0 serves as a generic/untyped identifier.

        {
            // Lock-free read path
            std::shared_lock<std::shared_mutex> lock(m_mutex);
            auto it = m_nameToId.find(typeName);
            if (it != m_nameToId.end()) {
                return it->second;
            }
        }

        {
            // Write path
            std::unique_lock<std::shared_mutex> lock(m_mutex);
            // Double-checked locking
            auto it = m_nameToId.find(typeName);
            if (it != m_nameToId.end()) {
                return it->second;
            }

            int newId = static_cast<int>(m_idToName.size());
            m_nameToId[typeName] = newId;
            m_idToName.push_back(typeName);
            return newId;
        }
    }

    /**
     * @brief Gets the string type name mapped to the given ID.
     * 정수 ID에 해당하는 문자열 타입 반환.
     * @return The original type name, or empty string if invalid.
     */
    std::string getName(int typeId) const {
        if (typeId <= 0) return "";
        std::shared_lock<std::shared_mutex> lock(m_mutex);
        if (static_cast<size_t>(typeId) < m_idToName.size()) {
            return m_idToName[typeId];
        }
        return "";
    }

    /**
     * @brief Creates an O(1) bitmask array configured to filter the given types.
     * O(1) 필터링에 사용할 비트마스크 벡터(선택된 타입을 1로 마킹)를 생성합니다.
     */
    std::vector<bool> createFilterMask(std::initializer_list<std::string> types) {
        std::vector<int> ids;
        for (const auto& t : types) {
            ids.push_back(resolve(t));
        }
        return createFilterMask(ids);
    }

    std::vector<bool> createFilterMask(const std::vector<int>& typeIds) {
        std::shared_lock<std::shared_mutex> lock(m_mutex);
        std::vector<bool> mask(m_idToName.size(), false);
        for (int id : typeIds) {
            if (id >= 0 && static_cast<size_t>(id) < mask.size()) {
                mask[id] = true;
            }
        }
        // Always ensure mask[0] matches expectations if explicitly passed, though usually 0 is un-filtered.
        return mask;
    }

private:
    TypeRegistry() {
        // ID 0 represents the "default/untagged" type
        m_idToName.push_back(""); 
    }
    ~TypeRegistry() = default;

    mutable std::shared_mutex m_mutex;
    std::unordered_map<std::string, int> m_nameToId;
    std::vector<std::string> m_idToName;
};

// Global accessors mapping strings via the singleton
inline int type_id(const std::string& name) { return TypeRegistry::instance().resolve(name); }
inline std::string type_name(int id) { return TypeRegistry::instance().getName(id); }

} // namespace nsTree
